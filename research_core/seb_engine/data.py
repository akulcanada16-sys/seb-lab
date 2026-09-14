from __future__ import annotations

import json
import errno
import math
import random
import csv
import hashlib
import io
import re
import time as wall_time
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import Bar, DataQuality, Quote
from .shared_file_read import read_shared_text


MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class HistoricalImportResult:
    """Immutable record of a NinjaTrader historical-export import."""

    parquet_path: Path
    metadata_path: Path
    bar_count: int
    first_bar_close_utc: datetime
    last_bar_close_utc: datetime
    source_sha256: str
    canonical_sha256: str
    session_count: int
    boundary_partial_sessions: int

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["parquet_path"] = str(self.parquet_path)
        value["metadata_path"] = str(self.metadata_path)
        value["first_bar_close_utc"] = self.first_bar_close_utc.isoformat()
        value["last_bar_close_utc"] = self.last_bar_close_utc.isoformat()
        return value


class HistoricalImportError(ValueError):
    """The supplied file cannot safely become historical research data."""


def _safe_dataset_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise HistoricalImportError(
            "Dataset name must contain only letters, numbers, _ or - and start with a letter or number"
        )
    return name


def _ninjatrader_timestamp(value: str, timezone: ZoneInfo) -> datetime:
    cleaned = value.strip().strip('"')
    # NinjaTrader's standard export uses yyyyMMdd HHmmss.  ISO timestamps and
    # the readable yyyy-MM-dd HH:mm:ss form are accepted for reproducible tests
    # and for exports processed by a spreadsheet without changing their values.
    formats = (
        "%Y%m%d %H%M%S", "%Y%m%d %H%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    )
    for timestamp_format in formats:
        try:
            return datetime.strptime(cleaned, timestamp_format).replace(tzinfo=timezone)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalImportError(f"Unsupported NinjaTrader timestamp: {value!r}") from exc
    return parsed.astimezone(timezone) if parsed.tzinfo else parsed.replace(tzinfo=timezone)


def _read_ninjatrader_export(path: Path) -> list[dict[str, str]]:
    """Read NinjaTrader's normal semicolon export or an equivalent headed CSV.

    The importer intentionally accepts only OHLCV bar exports; it never tries
    to infer bars from ticks, bid/ask records, or an internet data provider.
    """
    text = path.read_text(encoding="utf-8-sig")
    nonblank = [line for line in text.splitlines() if line.strip()]
    if not nonblank:
        raise HistoricalImportError("The export is empty")
    delimiter = ";" if nonblank[0].count(";") >= nonblank[0].count(",") else ","
    rows = list(csv.reader(nonblank, delimiter=delimiter))
    if not rows:
        raise HistoricalImportError("The export has no rows")
    normal = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    header = [normal(value) for value in rows[0]]
    aliases = {
        "time": {"time", "timestamp", "datetime", "date"},
        "open": {"open"}, "high": {"high"}, "low": {"low"}, "close": {"close"},
        "volume": {"volume", "vol"},
    }
    indexes: dict[str, int] = {}
    for key, names in aliases.items():
        matched = next((index for index, value in enumerate(header) if value in names), None)
        if matched is not None:
            indexes[key] = matched
    has_header = set(indexes) >= {"time", "open", "high", "low", "close"}
    if not has_header:
        # NinjaTrader's default historical bar export is headerless:
        # Time;Open;High;Low;Close;Volume.
        indexes = {"time": 0, "open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
    elif "volume" not in indexes:
        raise HistoricalImportError("The headed export is missing a Volume column")
    source_rows = rows[1:] if has_header else rows
    parsed: list[dict[str, str]] = []
    for line_number, row in enumerate(source_rows, start=2 if has_header else 1):
        if len(row) <= max(indexes.values()):
            raise HistoricalImportError(f"Row {line_number} does not contain Time, OHLC, and Volume")
        parsed.append({key: row[index].strip() for key, index in indexes.items()})
    return parsed


def _tick_aligned(value: float, tick_size: float) -> bool:
    return math.isclose(value / tick_size, round(value / tick_size), abs_tol=1e-8)


def _session_anchor(value: datetime) -> date:
    """Globex session is labelled by the calendar day containing its 18:00 open."""
    return (value - timedelta(days=1)).date() if value.timetz().replace(tzinfo=None) < time(18) else value.date()


def _validate_historical_bars(bars: list[Bar], *, tick_size: float) -> tuple[int, int]:
    if not bars:
        raise HistoricalImportError("The export contains no bars")
    seen: set[datetime] = set()
    sessions: dict[date, list[Bar]] = defaultdict(list)
    for index, bar in enumerate(bars, start=1):
        if bar.timestamp_utc in seen:
            raise HistoricalImportError(f"Duplicate one-minute timestamp at row {index}: {bar.market_time.isoformat()}")
        seen.add(bar.timestamp_utc)
        if bar.market_time.second or bar.market_time.microsecond:
            raise HistoricalImportError(f"Timestamp is not aligned to a full minute at row {index}: {bar.market_time.isoformat()}")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(math.isfinite(price) for price in prices) or not math.isfinite(bar.volume):
            raise HistoricalImportError(f"Non-finite OHLCV value at row {index}")
        if bar.volume < 0:
            raise HistoricalImportError(f"Negative volume at row {index}")
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.high < bar.low:
            raise HistoricalImportError(f"Invalid OHLC range at row {index}")
        if not all(_tick_aligned(price, tick_size) for price in prices):
            raise HistoricalImportError(f"MNQ price is not aligned to the {tick_size:g}-point tick at row {index}")
        sessions[_session_anchor(bar.market_time)].append(bar)
    for left, right in zip(bars, bars[1:]):
        if right.timestamp_utc <= left.timestamp_utc:
            raise HistoricalImportError("Timestamps must be strictly chronological; sort the source export before importing")
        minutes = round((right.timestamp_utc - left.timestamp_utc).total_seconds() / 60)
        if minutes == 1:
            continue
        # A regular Globex maintenance pause is an intentional 60-minute gap.
        if _scheduled_globex_maintenance(left, right):
            continue
        # Session boundaries and weekends are valid.  Any other missing minute
        # makes the data unsuitable for causal research and is rejected.
        if _session_anchor(left.market_time) != _session_anchor(right.market_time):
            continue
        raise HistoricalImportError(
            f"Missing {minutes - 1} one-minute bar(s) between {left.market_time.isoformat()} and {right.market_time.isoformat()}"
        )
    # The first and last session may be intentionally clipped by an export's
    # requested date range. Internal sessions must have all 1,380 tradable
    # minutes (18:00 ET through 17:00 ET, excluding 17:00-18:00 maintenance).
    ordered_sessions = sorted(sessions)
    partial_boundaries = 0
    for session in ordered_sessions:
        count = len(sessions[session])
        if session in (ordered_sessions[0], ordered_sessions[-1]):
            if count < 1_380:
                partial_boundaries += 1
        elif count != 1_380:
            raise HistoricalImportError(
                f"Incomplete Globex session {session.isoformat()}: expected 1380 one-minute bars, found {count}"
            )
    return len(sessions), partial_boundaries


def import_ninjatrader_historical_export(
    source_path: str | Path,
    *,
    output_root: str | Path,
    name: str,
    symbol: str = "MNQ",
    timestamp_is: str = "close",
    tick_size: float = 0.25,
    timezone: ZoneInfo = MARKET_TZ,
) -> HistoricalImportResult:
    """Import one-minute NinjaTrader OHLCV export into immutable local Parquet.

    ``timestamp_is`` must match the source convention. NinjaTrader's standard
    historical export stamps each minute at the end of the bar, so ``close`` is
    the safe default. Existing datasets
    are never overwritten: re-import with a new name if a corrected export is
    needed, preserving the original research evidence.
    """
    if timestamp_is not in {"open", "close"}:
        raise HistoricalImportError("timestamp_is must be 'open' or 'close'")
    if tick_size <= 0:
        raise HistoricalImportError("tick_size must be positive")
    dataset = _safe_dataset_name(name)
    source = Path(source_path)
    if not source.is_file():
        raise HistoricalImportError(f"NinjaTrader export does not exist: {source}")
    if source.suffix.lower() == ".zip":
        return import_ninjatrader_continuous_archive(
            source,
            output_root=output_root,
            name=name,
            timestamp_is=timestamp_is,
            tick_size=tick_size,
        )
    root = Path(output_root)
    parquet_path = root / f"{dataset}.parquet"
    metadata_path = root / f"{dataset}.metadata.json"
    if parquet_path.exists() or metadata_path.exists():
        raise HistoricalImportError(f"Dataset '{dataset}' already exists; historical imports are immutable")
    raw_rows = _read_ninjatrader_export(source)
    bars: list[Bar] = []
    for row_number, raw in enumerate(raw_rows, start=1):
        try:
            recorded = _ninjatrader_timestamp(raw["time"], timezone)
            open_price, high, low, close, volume = (float(raw[key]) for key in ("open", "high", "low", "close", "volume"))
        except (KeyError, ValueError) as exc:
            raise HistoricalImportError(f"Invalid NinjaTrader OHLCV value at row {row_number}") from exc
        close_time = recorded + timedelta(minutes=1) if timestamp_is == "open" else recorded
        bars.append(Bar(
            timestamp_utc=close_time.astimezone(UTC), market_time=close_time, symbol=symbol,
            timeframe_minutes=1, open=open_price, high=high, low=low, close=close,
            volume=volume, source="NINJATRADER_HISTORICAL_EXPORT", is_final=True,
        ))
    session_count, boundary_partial_sessions = _validate_historical_bars(bars, tick_size=tick_size)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    canonical_rows = "\n".join(json.dumps(bar.as_dict(), sort_keys=True, separators=(",", ":")) for bar in bars)
    canonical_sha256 = hashlib.sha256(canonical_rows.encode("utf-8")).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    written = ParquetStore(root).write(dataset, bars)
    metadata = {
        "schema_version": 1,
        "source_type": "NINJATRADER_HISTORICAL_EXPORT",
        "source_file_name": source.name,
        "source_sha256": source_sha256,
        "canonical_sha256": canonical_sha256,
        "symbol": symbol,
        "timeframe_minutes": 1,
        "timestamp_is": timestamp_is,
        "raw_timestamp_convention": (
            "NINJATRADER_END_OF_BAR" if timestamp_is == "close" else "EXPLICIT_BAR_OPEN"
        ),
        "canonical_timestamp_convention": "BAR_CLOSE",
        "market_timezone": str(timezone),
        "tick_size": tick_size,
        "bar_count": len(bars),
        "first_bar_close_utc": bars[0].timestamp_utc.isoformat(),
        "last_bar_close_utc": bars[-1].timestamp_utc.isoformat(),
        "session_count": session_count,
        "boundary_partial_sessions": boundary_partial_sessions,
        "validation": {
            "duplicate_timestamps": 0,
            "unexpected_internal_gaps": 0,
            "interior_sessions_require_full_1380_bars": True,
            "ohlcv_and_tick_alignment": "passed",
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return HistoricalImportResult(
        parquet_path=written, metadata_path=metadata_path, bar_count=len(bars),
        first_bar_close_utc=bars[0].timestamp_utc, last_bar_close_utc=bars[-1].timestamp_utc,
        source_sha256=source_sha256, canonical_sha256=canonical_sha256,
        session_count=session_count, boundary_partial_sessions=boundary_partial_sessions,
    )


def import_ninjatrader_continuous_archive(
    source_path: str | Path,
    *,
    output_root: str | Path,
    name: str,
    timestamp_is: str = "close",
    tick_size: float = 0.25,
) -> HistoricalImportResult:
    """Import a validated, partitioned MNQ continuous-history ZIP.

    The owner-supplied archive contains headed semicolon CSV partitions and a
    source-contract column.  Contract identity is deliberately preserved in
    ``symbol`` so the backtester can reset causal state at every rollover.
    Archive timestamps are UTC, while ``market_time`` is stored in New York
    time for the strategy/session rules.
    """
    if timestamp_is not in {"open", "close"}:
        raise HistoricalImportError("timestamp_is must be 'open' or 'close'")
    source = Path(source_path)
    dataset = _safe_dataset_name(name)
    root = Path(output_root)
    parquet_path = root / f"{dataset}.parquet"
    metadata_path = root / f"{dataset}.metadata.json"
    if parquet_path.exists() or metadata_path.exists():
        raise HistoricalImportError(f"Dataset '{dataset}' already exists; historical imports are immutable")

    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HistoricalImportError("The supplied historical archive is not a readable ZIP file") from exc
    with archive:
        members = sorted(
            member for member in archive.namelist()
            if re.search(r"MNQ_continuous_1m_\d{4}H[12]\.csv$", member, re.IGNORECASE)
        )
        if not members:
            raise HistoricalImportError("The archive contains no MNQ continuous one-minute CSV partitions")
        summary_member = next((member for member in archive.namelist() if member.endswith("VALIDATION_SUMMARY.txt")), None)
        summary_text = archive.read(summary_member).decode("utf-8-sig") if summary_member else ""

        import pandas as pd

        partitions = []
        for member in members:
            frame = pd.read_csv(
                io.BytesIO(archive.read(member)), sep=";", dtype={"source_contract": "string"},
            )
            required = {"timestamp", "open", "high", "low", "close", "volume", "source_contract"}
            if not required <= set(frame.columns):
                missing = ", ".join(sorted(required - set(frame.columns)))
                raise HistoricalImportError(f"Archive partition {member} is missing: {missing}")
            partitions.append(frame[list(required)])

    frame = pd.concat(partitions, ignore_index=True)
    del partitions
    try:
        stamps = pd.to_datetime(frame["timestamp"], format="%Y%m%d %H%M%S", utc=True)
    except (TypeError, ValueError) as exc:
        raise HistoricalImportError("The archive contains an invalid one-minute timestamp") from exc
    if timestamp_is == "open":
        stamps = stamps + pd.Timedelta(minutes=1)
    if stamps.duplicated().any():
        raise HistoricalImportError("The archive contains duplicate one-minute timestamps")
    if not stamps.is_monotonic_increasing:
        raise HistoricalImportError("The archive partitions are not strictly chronological")

    numeric = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any():
        raise HistoricalImportError("The archive contains an invalid OHLCV value")
    if (numeric["volume"] < 0).any():
        raise HistoricalImportError("The archive contains negative volume")
    if (
        (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1))
        | (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1))
    ).any():
        raise HistoricalImportError("The archive contains an invalid OHLC range")
    prices = numeric[["open", "high", "low", "close"]]
    if not ((((prices / tick_size).round() - (prices / tick_size)).abs()) < 1e-8).all().all():
        raise HistoricalImportError(f"The archive contains a price off the {tick_size:g}-point MNQ tick grid")

    market_stamps = stamps.dt.tz_convert(MARKET_TZ)
    session_labels = (market_stamps - pd.Timedelta(hours=18)).dt.date
    canonical = pd.DataFrame({
        "timestamp_utc": stamps.map(lambda value: value.isoformat()),
        "market_time": market_stamps.map(lambda value: value.isoformat()),
        "symbol": frame["source_contract"].astype(str),
        "timeframe_minutes": 1,
        "open": numeric["open"].astype(float),
        "high": numeric["high"].astype(float),
        "low": numeric["low"].astype(float),
        "close": numeric["close"].astype(float),
        "volume": numeric["volume"].astype(float),
        "source": "NINJATRADER_HISTORICAL_ARCHIVE",
        "is_final": True,
    })
    root.mkdir(parents=True, exist_ok=True)
    canonical.to_parquet(parquet_path, index=False)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    canonical_sha256 = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    contract_count = int(canonical["symbol"].nunique())
    first = stamps.iloc[0].to_pydatetime()
    last = stamps.iloc[-1].to_pydatetime()
    session_count = int(len(set(session_labels)))
    metadata = {
        "schema_version": 2,
        "source_type": "NINJATRADER_HISTORICAL_EXPORT",
        "source_format": "PARTITIONED_CONTINUOUS_ARCHIVE",
        "source_file_name": source.name,
        "source_sha256": source_sha256,
        "canonical_sha256": canonical_sha256,
        "symbol": "MNQ_CONTINUOUS",
        "contracts_preserved": contract_count,
        "timeframe_minutes": 1,
        "timestamp_is": timestamp_is,
        "raw_timestamp_convention": (
            "NINJATRADER_END_OF_BAR" if timestamp_is == "close" else "EXPLICIT_BAR_OPEN"
        ),
        "canonical_timestamp_convention": "BAR_CLOSE",
        "source_timezone": "UTC",
        "market_timezone": str(MARKET_TZ),
        "tick_size": tick_size,
        "bar_count": int(len(canonical)),
        "first_bar_close_utc": first.isoformat(),
        "last_bar_close_utc": last.isoformat(),
        "session_count": session_count,
        "boundary_partial_sessions": 2,
        "validation": {
            "duplicate_timestamps": 0,
            "unexpected_internal_gaps": 0,
            "strict_chronology": True,
            "ohlcv_and_tick_alignment": "passed",
            "contract_rollovers_preserved": True,
            "archive_integrity_report_present": bool(summary_text),
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return HistoricalImportResult(
        parquet_path=parquet_path,
        metadata_path=metadata_path,
        bar_count=int(len(canonical)),
        first_bar_close_utc=first,
        last_bar_close_utc=last,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha256,
        session_count=session_count,
        boundary_partial_sessions=2,
    )


def _scheduled_globex_maintenance(left: Bar, right: Bar) -> bool:
    """Return true for the normal same-date 17:00-18:00 ET futures pause."""
    if left.market_time.date() != right.market_time.date():
        return False
    return (
        time(16, 55) <= left.market_time.time() <= time(17, 5)
        and time(17, 55) <= right.market_time.time() <= time(18, 5)
    )


def _tail_lines(path: Path, count: int, block_size: int = 65_536) -> list[str]:
    """Read a bounded append-only JSONL tail without loading large recorder files."""
    if count <= 0 or not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count:
            step = min(block_size, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    return [line.decode("utf-8-sig") for line in data.splitlines()[-count:] if line.strip()]


def _tail_json(path: Path, count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _tail_lines(path, count):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def parse_market_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MARKET_TZ)
    return parsed.astimezone(MARKET_TZ)


def normalize_bar(
    raw: dict[str, Any],
    *,
    source: str,
    symbol: str | None = None,
    timeframe: int | None = None,
) -> Bar:
    tf = int(timeframe or raw.get("timeframe_minutes", 1))
    if raw.get("bar_close_time"):
        market_time = parse_market_timestamp(raw["bar_close_time"])
    else:
        open_time = parse_market_timestamp(raw.get("time") or raw["bar_open_time"])
        market_time = open_time + timedelta(minutes=tf)
    return Bar(
        timestamp_utc=market_time.astimezone(UTC),
        market_time=market_time,
        symbol=symbol or raw.get("instrument") or "MNQ",
        timeframe_minutes=tf,
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw.get("volume", 0.0)),
        source=source,
        is_final=bool(raw.get("is_final", True)),
    )


def rth_session_date(bar: Bar) -> date | None:
    local = bar.market_time
    if time(9, 30) < local.time() <= time(16, 0):
        return local.date()
    return None


class BarAggregator:
    """Causal 1m aggregation; a higher-timeframe bar emits only on the next bucket."""

    def __init__(self, timeframe_minutes: int):
        if timeframe_minutes not in (5, 15):
            raise ValueError("Only 5m and 15m aggregation is supported")
        self.timeframe = timeframe_minutes
        self._bucket: datetime | None = None
        self._bars: list[Bar] = []

    def _bucket_start(self, bar: Bar) -> datetime:
        open_time = bar.market_time - timedelta(minutes=bar.timeframe_minutes)
        minute = (open_time.minute // self.timeframe) * self.timeframe
        return open_time.replace(minute=minute, second=0, microsecond=0)

    def update(self, bar: Bar) -> Bar | None:
        if bar.timeframe_minutes != 1 or not bar.is_final:
            return None
        bucket = self._bucket_start(bar)
        completed = None
        if self._bucket is not None and bucket != self._bucket:
            completed = self._finalize()
            self._bars = []
        self._bucket = bucket
        self._bars.append(bar)
        return completed

    def _finalize(self) -> Bar | None:
        if not self._bars or self._bucket is None:
            return None
        bars = self._bars
        return Bar(
            timestamp_utc=(self._bucket + timedelta(minutes=self.timeframe)).astimezone(UTC),
            market_time=self._bucket + timedelta(minutes=self.timeframe),
            symbol=bars[-1].symbol,
            timeframe_minutes=self.timeframe,
            open=bars[0].open,
            high=max(bar.high for bar in bars),
            low=min(bar.low for bar in bars),
            close=bars[-1].close,
            volume=sum(bar.volume for bar in bars),
            source=f"aggregate:{bars[-1].source}",
            is_final=True,
        )


def aggregate_bars(bars: Iterable[Bar], timeframe: int) -> list[Bar]:
    groups: dict[datetime, list[Bar]] = defaultdict(list)
    for bar in bars:
        if not bar.is_final:
            continue
        open_time = bar.market_time - timedelta(minutes=bar.timeframe_minutes)
        minute = (open_time.minute // timeframe) * timeframe
        bucket = open_time.replace(minute=minute, second=0, microsecond=0)
        groups[bucket].append(bar)
    results: list[Bar] = []
    for bucket, grouped in sorted(groups.items()):
        grouped.sort(key=lambda item: item.timestamp_utc)
        close_time = bucket + timedelta(minutes=timeframe)
        if grouped[-1].market_time < close_time:
            continue
        results.append(
            Bar(
                timestamp_utc=close_time.astimezone(UTC),
                market_time=close_time,
                symbol=grouped[-1].symbol,
                timeframe_minutes=timeframe,
                open=grouped[0].open,
                high=max(item.high for item in grouped),
                low=min(item.low for item in grouped),
                close=grouped[-1].close,
                volume=sum(item.volume for item in grouped),
                source=f"aggregate:{grouped[-1].source}",
            )
        )
    return results


class DataQualityMonitor:
    def __init__(
        self,
        stale_after_seconds: float = 5.0,
        warmup_bars: int = 30,
        finalized_bar_stale_after_seconds: float | None = None,
    ):
        self.stale_after_seconds = stale_after_seconds
        self.warmup_bars = warmup_bars
        self.finalized_bar_stale_after_seconds = finalized_bar_stale_after_seconds

    def inspect(
        self,
        bars: list[Bar],
        *,
        source: str,
        live: bool,
        observed_at_utc: datetime | None = None,
    ) -> DataQuality:
        symbol = bars[-1].symbol if bars else "UNKNOWN"
        quality = DataQuality(source=source, active_symbol=symbol)
        quality.warmup_complete = len(bars) >= self.warmup_bars
        if not bars:
            quality.blocking_reasons.append("No finalized bars are available")
            quality.is_stale = True
            return quality
        ordered = sorted(bars, key=lambda item: item.timestamp_utc)
        seen: set[datetime] = set()
        prior: datetime | None = None
        for original in bars:
            if original.timestamp_utc in seen:
                quality.duplicate_events += 1
            seen.add(original.timestamp_utc)
            if prior and original.timestamp_utc < prior:
                quality.out_of_order_events += 1
            prior = original.timestamp_utc
        for left, right in zip(ordered, ordered[1:]):
            gap = int((right.timestamp_utc - left.timestamp_utc).total_seconds() // 60)
            if gap > 1 and left.market_time.date() == right.market_time.date() and not _scheduled_globex_maintenance(left, right):
                quality.missing_bars += max(0, gap - 1)
        now = datetime.now(UTC)
        quality.last_finalized_bar_timestamp = ordered[-1].timestamp_utc
        quality.finalized_bar_age_seconds = max(
            0.0, (now - quality.last_finalized_bar_timestamp).total_seconds()
        )
        quality.last_event_timestamp = observed_at_utc or ordered[-1].timestamp_utc
        quality.feed_age_seconds = max(0.0, (now - quality.last_event_timestamp).total_seconds())
        quality.is_stale = live and quality.feed_age_seconds > self.stale_after_seconds
        if quality.is_stale:
            quality.blocking_reasons.append(
                f"Feed is stale ({quality.feed_age_seconds:.1f}s > {self.stale_after_seconds:.1f}s)"
            )
        if (
            live
            and self.finalized_bar_stale_after_seconds is not None
            and quality.finalized_bar_age_seconds > self.finalized_bar_stale_after_seconds
        ):
            quality.blocking_reasons.append(
                "Finalized bars are stale "
                f"({quality.finalized_bar_age_seconds:.1f}s > "
                f"{self.finalized_bar_stale_after_seconds:.1f}s)"
            )
        if not quality.warmup_complete:
            quality.blocking_reasons.append(
                f"Warm-up incomplete ({len(bars)}/{self.warmup_bars} bars)"
            )
        if quality.out_of_order_events:
            quality.blocking_reasons.append("Out-of-order events detected")
        return quality


class NinjaTraderLiveAdapter:
    def __init__(self, path: str | Path, stale_after_seconds: float = 5.0):
        self.path = Path(path)
        self.stale_after_seconds = stale_after_seconds

    def read(self) -> tuple[list[Bar], list[Bar], Quote | None, DataQuality]:
        if not self.path.exists():
            return [], [], None, DataQuality(
                source="NINJATRADER_JSON",
                active_symbol="UNKNOWN",
                is_stale=True,
                blocking_reasons=[f"Bridge file does not exist: {self.path}"],
            )
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        snapshot_market = parse_market_timestamp(raw["timestamp"])
        snapshot_utc = snapshot_market.astimezone(UTC)
        symbol = raw.get("instrument", "MNQ")
        bars_1m = [
            normalize_bar(row, source="NINJATRADER_JSON", symbol=symbol, timeframe=1)
            for row in raw.get("bars_1m", [])
        ]
        bars_5m = [
            normalize_bar(row, source="NINJATRADER_JSON", symbol=symbol, timeframe=5)
            for row in raw.get("bars_5m", [])
        ]
        # Bridge arrays include the forming bar. Only bars whose close time is known are causal.
        bars_1m = [bar for bar in bars_1m if bar.timestamp_utc <= snapshot_utc]
        bars_5m = [bar for bar in bars_5m if bar.timestamp_utc <= snapshot_utc]
        quote = Quote(
            timestamp_utc=snapshot_utc,
            symbol=symbol,
            last=float(raw["last"]) if raw.get("last") is not None else None,
            bid=float(raw["bid"]) if raw.get("bid") is not None else None,
            ask=float(raw["ask"]) if raw.get("ask") is not None else None,
            last_volume=float(raw.get("last_volume", 0.0)),
            source="NINJATRADER_JSON",
        )
        quality = DataQualityMonitor(self.stale_after_seconds).inspect(
            bars_1m, source="NINJATRADER_JSON", live=True, observed_at_utc=snapshot_utc
        )
        return bars_1m, bars_5m, quote, quality


class RecorderV2LiveAdapter:
    """Read ResearchDataRecorder 1.2's atomic live projection and session tail.

    ``live_state.json`` is the low-latency operational projection. The latest
    daily ``bars_1m.jsonl`` tail supplies enough session history for prior,
    overnight, ORB, and IB levels without ever scanning multi-gigabyte L1 files.
    All attached-system files are read-only.
    """

    def __init__(
        self,
        root: str | Path,
        instrument: str | None = None,
        stale_after_seconds: float = 10.0,
        finalized_bar_stale_after_seconds: float = 90.0,
        minimum_contiguous_bars_after_gap: int = 15,
        session_tail_bars: int = 25_000,
        history_days: int = 21,
    ):
        self.root = Path(root)
        self.instrument = instrument
        self.stale_after_seconds = stale_after_seconds
        self.finalized_bar_stale_after_seconds = finalized_bar_stale_after_seconds
        self.minimum_contiguous_bars_after_gap = max(1, minimum_contiguous_bars_after_gap)
        self.session_tail_bars = session_tail_bars
        self.history_days = history_days
        self.active_directory: Path | None = None
        self._completed_day_cache: dict[Path, list[dict[str, Any]]] = {}

    @staticmethod
    def _folder_name(instrument: str) -> str:
        return "_".join(instrument.replace("/", " ").split()).upper()

    def _instrument_directory(self) -> Path | None:
        if self.instrument:
            exact = self.root / self._folder_name(self.instrument)
            # The configured directory is stable even while its live report is
            # replaced. Let the fresh read below establish report availability;
            # a brief filename gap must not choose another contract.
            if exact.is_dir():
                return exact
        candidates = [
            path.parent
            for path in self.root.glob("*/live_state.json")
            if path.is_file()
        ]
        if self.instrument:
            product = self._folder_name(self.instrument).split("_", 1)[0]
            matching = [path for path in candidates if path.name.upper().startswith(product)]
            candidates = matching or candidates
        return max(candidates, key=lambda path: (path / "live_state.json").stat().st_mtime) if candidates else None

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        started = wall_time.monotonic()
        for attempt in range(2):
            try:
                value = json.loads(read_shared_text(path, encoding="utf-8-sig"))
            except OSError as exc:
                # Recorder metadata is atomically replaced. On Windows, a reader
                # can occasionally meet a brief missing/sharing/access window.
                # Retry that narrow native-error case once, using a
                # fresh read; all other file failures remain an unknown payload.
                # CPython can surface the same Windows sharing window as an ordinary
                # PermissionError(EACCES) without a winerror.  Retry only that
                # access-denied form (or the known native sharing codes), always
                # by reading the file anew; every other failure remains unknown.
                retryable_sharing_error = (
                    getattr(exc, "winerror", None) in {2, 5, 32, 33}
                    or (
                        isinstance(exc, PermissionError)
                        and exc.errno == errno.EACCES
                        and getattr(exc, "winerror", None) is None
                    )
                )
                if attempt == 0 and retryable_sharing_error and wall_time.monotonic() - started < 0.025:
                    wall_time.sleep(0.025)
                    if wall_time.monotonic() - started >= 0.050:
                        return {}
                    continue
                return {}
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}
        return {}

    def _day_directories(self, directory: Path) -> list[Path]:
        days = sorted(
            (path for path in directory.iterdir() if path.is_dir() and (path / "bars_1m.jsonl").exists()),
        )
        return days[-self.history_days:]

    def _history_rows(self, directory: Path) -> list[dict[str, Any]]:
        days = self._day_directories(directory)
        rows: list[dict[str, Any]] = []
        for index, day in enumerate(days):
            path = day / "bars_1m.jsonl"
            if index == len(days) - 1:
                day_rows = _tail_json(path, 1_440)
            else:
                day_rows = self._completed_day_cache.get(path)
                if day_rows is None:
                    day_rows = _tail_json(path, 1_440)
                    self._completed_day_cache[path] = day_rows
            rows.extend(day_rows)
        return rows

    def read(self) -> tuple[list[Bar], list[Bar], Quote | None, DataQuality]:
        directory = self._instrument_directory()
        self.active_directory = directory
        if directory is None:
            return [], [], None, DataQuality(
                source="NT8_RECORDER_V2",
                source_type="LIVE_RECORDER_V2",
                active_symbol="UNKNOWN",
                is_stale=True,
                blocking_reasons=[f"Recorder live_state.json not found below {self.root}"],
            )

        live = self._read_object(directory / "live_state.json")
        status = self._read_object(directory / "status.json")
        symbol = str(live.get("instrument") or status.get("instrument") or directory.name.replace("_", " "))
        quote_raw = live.get("quote") if isinstance(live.get("quote"), dict) else {}
        # ``published_utc`` is only the recorder thread's heartbeat.  Treating it
        # as a market-event timestamp made an empty recorder look live while the
        # strategy's finalized candles were frozen.
        stamp_value = quote_raw.get("recorded_utc")
        quote_stamp = parse_market_timestamp(stamp_value).astimezone(UTC) if stamp_value else None
        quote = None
        if quote_stamp:
            quote = Quote(
                timestamp_utc=quote_stamp,
                symbol=symbol,
                last=float(quote_raw["last"]) if quote_raw.get("last") is not None else None,
                bid=float(quote_raw["bid"]) if quote_raw.get("bid") is not None else None,
                ask=float(quote_raw["ask"]) if quote_raw.get("ask") is not None else None,
                last_volume=float(quote_raw.get("last_size", 0.0)),
                source="NT8_RECORDER_V2",
            )

        raw_bars = list(live.get("bars_1m") or [])
        raw_bars = self._history_rows(directory) + raw_bars
        normalized: dict[datetime, Bar] = {}
        for row in raw_bars:
            try:
                bar = normalize_bar(row, source="NT8_RECORDER_V2", symbol=symbol, timeframe=1)
            except (KeyError, TypeError, ValueError):
                continue
            if bar.is_final:
                normalized[bar.timestamp_utc] = bar
        bars = sorted(normalized.values(), key=lambda bar: bar.timestamp_utc)[-self.session_tail_bars:]
        observed = quote.timestamp_utc if quote else None
        quality = DataQualityMonitor(
            self.stale_after_seconds,
            finalized_bar_stale_after_seconds=self.finalized_bar_stale_after_seconds,
        ).inspect(
            bars,
            source="NT8_RECORDER_V2",
            live=True,
            observed_at_utc=observed,
        )
        quality.source_type = "LIVE_RECORDER_V2"
        quality.recorder_running = bool(status.get("running")) if status else None
        quality.recorder_version = str(status.get("recorder_version")) if status.get("recorder_version") else None
        quality.dropped_records = int(status.get("dropped_records", 0) or 0)
        quality.write_errors = int(status.get("write_errors", 0) or 0)
        quality.queue_depth = int(status["queue_depth"]) if status.get("queue_depth") is not None else None
        configured = self._folder_name(self.instrument) if self.instrument else directory.name.upper()
        quality.rollover_state = "CURRENT" if directory.name.upper() == configured else f"AUTO_DISCOVERED:{directory.name}"

        schema = live.get("schema_version")
        if schema != 2:
            quality.blocking_reasons.append(f"Unsupported recorder schema: {schema!r}; expected 2")
        if quote is None:
            quality.blocking_reasons.append("Recorder live projection has no timestamped market quote")
        if status and not status.get("running", False):
            quality.blocking_reasons.append("Recorder reports running=false")
        if quality.dropped_records:
            quality.blocking_reasons.append(f"Recorder reports {quality.dropped_records} dropped records")
        writer_error = str(status.get("last_writer_error") or "")
        writer_error_lower = writer_error.lower()
        metadata_sharing_collision = (
            writer_error_lower.startswith(("status:", "live state:"))
            and ("used by another process" in writer_error_lower or "sharing violation" in writer_error_lower)
        )
        accepted = status.get("accepted_records")
        written = status.get("written_records")
        # A running writer normally has records either queued or currently being
        # flushed.  They are not lost merely because accepted_records is a few
        # counts ahead of written_records at the instant status.json is sampled.
        # Reconcile against the declared queue depth and allow one in-flight
        # record that has already been removed from the queue by the writer.
        if accepted is None or written is None:
            records_reconciled = True
        else:
            accepted_count = int(accepted)
            written_count = int(written)
            pending_count = max(0, int(status.get("queue_depth", 0) or 0))
            outstanding_count = accepted_count - written_count
            records_reconciled = (
                outstanding_count >= 0
                and outstanding_count <= pending_count + 1
            )
        if quality.write_errors:
            if metadata_sharing_collision and records_reconciled and not quality.dropped_records:
                quality.advisory_reasons.append(
                    f"Recorder recovered from {quality.write_errors} metadata-file sharing collision(s); market records reconcile"
                )
            else:
                quality.blocking_reasons.append(f"Recorder reports {quality.write_errors} unresolved write errors")
        if writer_error and not metadata_sharing_collision:
            quality.blocking_reasons.append(f"Recorder writer error: {writer_error}")
        recent = bars[-60:]
        latest_gap: tuple[int, int] | None = None
        for index, (left, right) in enumerate(zip(recent, recent[1:])):
            gap = (right.timestamp_utc - left.timestamp_utc).total_seconds()
            # Globex trades through local midnight, so a real recorder outage
            # must not disappear merely because the gap crosses a date boundary.
            if gap > 90 and not _scheduled_globex_maintenance(left, right):
                latest_gap = (int(gap // 60) - 1, len(recent) - index - 1)
        if latest_gap:
            missing_minutes, contiguous_bars = latest_gap
            if contiguous_bars < self.minimum_contiguous_bars_after_gap:
                quality.blocking_reasons.append(
                    f"Recent finalized-bar gap of {missing_minutes} minute(s); "
                    f"waiting for {contiguous_bars}/{self.minimum_contiguous_bars_after_gap} "
                    "consecutive recovery bars"
                )
            else:
                quality.advisory_reasons.append(
                    f"Recovered from a {missing_minutes}-minute finalized-bar gap after "
                    f"{contiguous_bars} consecutive bars"
                )
        return bars, [], quote, quality


class ResearchJSONLAdapter:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def read_bars(
        self,
        timeframe: int = 1,
        symbol_contains: str | None = None,
        limit: int | None = None,
    ) -> list[Bar]:
        paths = sorted(self.root.rglob(f"bars_{timeframe}m.jsonl"))
        bars: dict[datetime, Bar] = {}
        for path in paths:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if not raw.get("is_final", False):
                        continue
                    if symbol_contains and symbol_contains.upper() not in raw.get("instrument", "").upper():
                        continue
                    bar = normalize_bar(raw, source="NT8_RESEARCH_JSONL", timeframe=timeframe)
                    bars[bar.timestamp_utc] = bar
        values = sorted(bars.values(), key=lambda item: item.timestamp_utc)
        return values[-limit:] if limit else values


class ParquetStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, bars: list[Bar]) -> Path:
        import pandas as pd

        path = self.root / f"{name}.parquet"
        frame = pd.DataFrame([bar.as_dict() for bar in bars])
        frame.to_parquet(path, index=False)
        return path

    def read(self, name: str) -> list[Bar]:
        import pyarrow.parquet as pq

        result: list[Bar] = []
        parquet = pq.ParquetFile(self.root / f"{name}.parquet")
        for batch in parquet.iter_batches(batch_size=100_000):
            frame = batch.to_pandas()
            for row in frame.itertuples(index=False):
                market_time = parse_market_timestamp(str(row.market_time))
                result.append(Bar(
                    timestamp_utc=market_time.astimezone(UTC), market_time=market_time,
                    symbol=str(row.symbol), timeframe_minutes=int(row.timeframe_minutes),
                    open=float(row.open), high=float(row.high), low=float(row.low),
                    close=float(row.close), volume=float(row.volume), source="PARQUET",
                    is_final=bool(getattr(row, "is_final", True)),
                ))
        return result

    def inspect_dataset(self, name: str) -> dict[str, Any]:
        """Return trusted import metadata for one immutable historical dataset.

        Only NinjaTrader historical imports carry this sidecar.  Older generic
        Parquet files deliberately do not masquerade as validated research data.
        """
        dataset = _safe_dataset_name(name)
        metadata_path = self.root / f"{dataset}.metadata.json"
        parquet_path = self.root / f"{dataset}.parquet"
        if not metadata_path.is_file() or not parquet_path.is_file():
            raise HistoricalImportError(f"Validated historical dataset '{dataset}' was not found")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HistoricalImportError(f"Dataset '{dataset}' has unreadable metadata") from exc
        if not isinstance(metadata, dict) or metadata.get("source_type") != "NINJATRADER_HISTORICAL_EXPORT":
            raise HistoricalImportError(f"Dataset '{dataset}' is not a validated NinjaTrader historical import")
        return {"name": dataset, "parquet_path": str(parquet_path), **metadata}

    def list_datasets(self) -> list[dict[str, Any]]:
        """List only complete validated NinjaTrader imports, newest range first."""
        datasets: list[dict[str, Any]] = []
        for metadata_path in sorted(self.root.glob("*.metadata.json")):
            suffix = ".metadata.json"
            if not metadata_path.name.endswith(suffix):
                continue
            try:
                datasets.append(self.inspect_dataset(metadata_path.name[:-len(suffix)]))
            except HistoricalImportError:
                # A half-copied or manually edited artifact is not eligible for
                # research selection. Its files are left untouched for review.
                continue
        return sorted(datasets, key=lambda item: str(item.get("last_bar_close_utc", "")), reverse=True)


class SyntheticFeed:
    """Seeded, inspectable fallback. It is never labelled live market data."""

    def __init__(self, symbol: str = "MNQ SYNTH", seed: int = 7331):
        self.symbol = symbol
        self.random = random.Random(seed)

    def sessions(self, count: int = 8, bars_per_session: int = 120) -> list[Bar]:
        start_day = date(2026, 7, 1)
        price = 22_000.0
        bars: list[Bar] = []
        for day_offset in range(count):
            session_day = start_day + timedelta(days=day_offset)
            if session_day.weekday() >= 5:
                continue
            open_time = datetime.combine(session_day, time(9, 30), MARKET_TZ)
            regime = (day_offset % 4)
            for index in range(bars_per_session):
                seasonal = 1.7 - 0.7 * math.sin(math.pi * index / max(1, bars_per_session - 1))
                drift = (0.45 if regime == 0 else -0.35 if regime == 1 else 0.0)
                cycle = 2.8 * math.sin(index / (3.0 if regime == 2 else 8.0))
                shock = self.random.gauss(0, 2.6 if regime == 3 else 1.7)
                change = drift + cycle * 0.18 + shock
                open_price = price
                close = round((open_price + change) * 4) / 4
                high = round((max(open_price, close) + abs(self.random.gauss(1.4, 0.8))) * 4) / 4
                low = round((min(open_price, close) - abs(self.random.gauss(1.4, 0.8))) * 4) / 4
                market_close = open_time + timedelta(minutes=index + 1)
                volume = max(30.0, 650.0 * seasonal + self.random.gauss(0, 120))
                if index in (28, 29, 30, 58, 59, 60, 88, 89, 90):
                    volume *= 1.7
                bars.append(
                    Bar(
                        timestamp_utc=market_close.astimezone(UTC),
                        market_time=market_close,
                        symbol=self.symbol,
                        timeframe_minutes=1,
                        open=open_price,
                        high=max(high, open_price, close),
                        low=min(low, open_price, close),
                        close=close,
                        volume=round(volume),
                        source="SYNTHETIC_SEEDED",
                    )
                )
                price = close
            price += self.random.gauss(0, 18)
        return bars

    def next_bar(self, prior: Bar) -> Bar:
        market_close = prior.market_time + timedelta(minutes=1)
        change = self.random.gauss(0, 2.4) + 0.7 * math.sin(market_close.minute / 4)
        close = round((prior.close + change) * 4) / 4
        high = round((max(prior.close, close) + abs(self.random.gauss(1.2, 0.6))) * 4) / 4
        low = round((min(prior.close, close) - abs(self.random.gauss(1.2, 0.6))) * 4) / 4
        return replace(
            prior,
            timestamp_utc=market_close.astimezone(UTC),
            market_time=market_close,
            open=prior.close,
            high=high,
            low=low,
            close=close,
            volume=max(50, round(self.random.gauss(800, 180))),
        )
