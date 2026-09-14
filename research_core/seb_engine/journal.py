from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import Bar, StrategyEvaluation, TradeTicket


SCHEMA_VERSION = 19


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    version_hash TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    semantic_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    is_live INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strategy_events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    state_from TEXT,
    state_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    data_source TEXT NOT NULL,
    data_freshness_json TEXT NOT NULL,
    checklist_json TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    reference_level_json TEXT,
    signal_id TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    quantity INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    ticket_json TEXT NOT NULL,
    snapshot_path TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    timestamp TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    fill_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    slippage REAL NOT NULL DEFAULT 0,
    external_id TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry REAL NOT NULL,
    exit REAL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    quantity INTEGER NOT NULL,
    direction TEXT NOT NULL,
    result_points REAL,
    result_r REAL,
    result_dollars REAL,
    mae REAL,
    mfe REAL,
    time_to_mae_bars INTEGER,
    time_to_mfe_bars INTEGER,
    exit_reason TEXT,
    context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    note TEXT NOT NULL,
    tags_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    signal_id TEXT,
    event_id TEXT,
    timestamp TEXT NOT NULL,
    path TEXT NOT NULL,
    feature_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    date_start TEXT,
    date_end TEXT,
    mode TEXT NOT NULL,
    parameter_json TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    split_metrics_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    backtest_trade_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
    signal_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    entry REAL NOT NULL,
    exit REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    direction TEXT NOT NULL,
    result_points REAL NOT NULL,
    result_r REAL NOT NULL,
    result_dollars REAL NOT NULL,
    mae REAL NOT NULL,
    mfe REAL NOT NULL,
    time_to_mae_bars INTEGER NOT NULL,
    time_to_mfe_bars INTEGER NOT NULL,
    bars_held INTEGER NOT NULL,
    first_hit TEXT NOT NULL,
    forward_returns_json TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    parameter_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_evolution (
    evolution_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    champion_version TEXT NOT NULL,
    challenger_version TEXT NOT NULL,
    changed_parameter TEXT NOT NULL,
    old_value_json TEXT NOT NULL,
    new_value_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision TEXT NOT NULL,
    baseline_run_id TEXT NOT NULL,
    challenger_run_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_change_epochs (
    epoch_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evolution_automation_runs (
    automation_id TEXT PRIMARY KEY,
    analysis_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    trigger_name TEXT NOT NULL,
    data_start TEXT,
    data_end TEXT,
    bars INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0,
    activated_count INTEGER NOT NULL DEFAULT 0,
    reviews_json TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS evolution_agent_state (
    strategy_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    current_version TEXT NOT NULL,
    active_hypothesis_id TEXT,
    last_trade_id TEXT,
    last_trade_count INTEGER NOT NULL DEFAULT 0,
    next_review_trade_count INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evolution_hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    analysis_id TEXT,
    epoch_id TEXT,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT,
    parameter TEXT,
    old_value_json TEXT,
    new_value_json TEXT,
    baseline_target INTEGER NOT NULL,
    experiment_target INTEGER NOT NULL,
    baseline_metrics_json TEXT NOT NULL,
    experiment_metrics_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    decision_reason TEXT,
    concluded_at TEXT
);
CREATE TABLE IF NOT EXISTS strategy_evidence_snapshots (
    evidence_snapshot_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_family_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    trigger_trade_id TEXT,
    created_at TEXT NOT NULL,
    closed_trades INTEGER NOT NULL,
    distinct_trading_days INTEGER NOT NULL,
    chronological_windows INTEGER NOT NULL,
    regime_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    subgroup_metrics_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    data_quality_json TEXT NOT NULL,
    identifiers_json TEXT NOT NULL,
    source_record_ids_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS strategy_weaknesses (
    weakness_id TEXT PRIMARY KEY,
    evidence_snapshot_id TEXT NOT NULL REFERENCES strategy_evidence_snapshots(evidence_snapshot_id),
    strategy_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    category TEXT NOT NULL,
    issue TEXT NOT NULL,
    affected_subgroup TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    effect_size REAL,
    uncertainty_json TEXT NOT NULL,
    chronological_stability REAL NOT NULL,
    regime_coverage INTEGER NOT NULL,
    supporting_metrics_json TEXT NOT NULL,
    conflicting_evidence_json TEXT NOT NULL,
    evidence_strength TEXT NOT NULL,
    rank_order INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_hypotheses_v2 (
    hypothesis_id TEXT PRIMARY KEY,
    normalized_signature TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_family_id TEXT NOT NULL,
    champion_version_id TEXT NOT NULL,
    evidence_snapshot_id TEXT NOT NULL REFERENCES strategy_evidence_snapshots(evidence_snapshot_id),
    observed_problem TEXT NOT NULL,
    evidence_summary_json TEXT NOT NULL,
    proposed_change_json TEXT NOT NULL,
    affected_component TEXT NOT NULL,
    change_category TEXT NOT NULL,
    expected_market_mechanism TEXT NOT NULL,
    expected_benefit TEXT NOT NULL,
    possible_downside TEXT NOT NULL,
    primary_metric TEXT NOT NULL,
    secondary_metrics_json TEXT NOT NULL,
    minimum_validation_sample INTEGER NOT NULL,
    promotion_criteria_json TEXT NOT NULL,
    rejection_criteria_json TEXT NOT NULL,
    severe_harm_stop_json TEXT NOT NULL,
    maximum_experiment_length INTEGER NOT NULL,
    confidence_label TEXT NOT NULL,
    implementation_spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    rejection_reason TEXT
);
CREATE TABLE IF NOT EXISTS strategy_challengers (
    challenger_version_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    parent_champion_version_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL REFERENCES strategy_hypotheses_v2(hypothesis_id),
    code_commit_hash TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    exact_changed_rules_json TEXT NOT NULL,
    unchanged_rules_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    required_market_data_json TEXT NOT NULL,
    experiment_status TEXT NOT NULL,
    correctness_json TEXT NOT NULL,
    immutable_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS strategy_experiments (
    experiment_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL REFERENCES strategy_hypotheses_v2(hypothesis_id),
    evidence_snapshot_id TEXT NOT NULL,
    champion_version_id TEXT NOT NULL,
    challenger_version_id TEXT NOT NULL REFERENCES strategy_challengers(challenger_version_id),
    status TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    activation_approval TEXT NOT NULL,
    promotion_approval TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cooldown_until_trade_count INTEGER NOT NULL DEFAULT 0,
    replay_json TEXT NOT NULL,
    experiment_plan_json TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    latest_evaluation_json TEXT NOT NULL,
    final_evidence_snapshot_id TEXT,
    final_result TEXT,
    final_reason TEXT,
    identifiers_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_experiment_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES strategy_experiments(experiment_id),
    strategy_id TEXT NOT NULL,
    market_time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    champion_candidate_id TEXT,
    challenger_candidate_id TEXT,
    champion_qualified INTEGER NOT NULL,
    challenger_qualified INTEGER NOT NULL,
    comparable INTEGER NOT NULL,
    champion_evaluation_json TEXT NOT NULL,
    challenger_evaluation_json TEXT NOT NULL,
    feature_snapshot_json TEXT NOT NULL,
    data_window_id TEXT NOT NULL,
    UNIQUE(experiment_id, market_time)
);
CREATE TABLE IF NOT EXISTS strategy_experiment_evidence_snapshots (
    experiment_evidence_snapshot_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES strategy_experiments(experiment_id),
    strategy_id TEXT NOT NULL,
    trigger_shadow_position_id TEXT,
    created_at TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    identifiers_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS strategy_shadow_positions (
    shadow_position_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES strategy_experiments(experiment_id),
    opportunity_id TEXT NOT NULL REFERENCES strategy_experiment_opportunities(opportunity_id),
    role TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    signal_time TEXT NOT NULL,
    entry_time TEXT,
    close_time TEXT,
    status TEXT NOT NULL,
    planned_entry REAL NOT NULL,
    entry_price REAL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    exit_price REAL,
    exit_reason TEXT,
    result_r REAL,
    result_points REAL,
    result_dollars REAL,
    mae REAL NOT NULL DEFAULT 0,
    mfe REAL NOT NULL DEFAULT 0,
    bars_held INTEGER NOT NULL DEFAULT 0,
    slippage_cost REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    context_json TEXT NOT NULL,
    UNIQUE(experiment_id, role, candidate_id)
);
CREATE TABLE IF NOT EXISTS strategy_evolution_state (
    strategy_id TEXT PRIMARY KEY,
    champion_version_id TEXT NOT NULL,
    active_experiment_id TEXT,
    phase TEXT NOT NULL,
    last_completed_experiment_trade_count INTEGER NOT NULL DEFAULT 0,
    last_learning_cycle_trade_count INTEGER NOT NULL DEFAULT 0,
    last_rejected_trade_count INTEGER NOT NULL DEFAULT 0,
    last_evidence_snapshot_id TEXT,
    last_closed_trade_id TEXT,
    last_reviewed_at TEXT,
    cooldown_until_trade_count INTEGER NOT NULL DEFAULT 0,
    completed_experiments_window_json TEXT NOT NULL,
    eligibility_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_evolution_audit (
    audit_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prior_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS strategy_evidence_invalidations (
    invalidation_id TEXT PRIMARY KEY,
    invalidated_at TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    UNIQUE(target_type, target_id, reason_code)
);
CREATE TABLE IF NOT EXISTS strategy_version_lineage (
    version_hash TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    activation_predecessor_version_id TEXT,
    statistical_parent_version_id TEXT,
    revision_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    starting_balance REAL NOT NULL,
    cash_balance REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    peak_equity REAL NOT NULL,
    max_drawdown REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    entry_threshold REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_strategy_retirements (
    strategy_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    preserved_closed_trades INTEGER NOT NULL,
    preserved_net_pnl REAL NOT NULL,
    account_adjustment REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_account_resets (
    reset_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    reset_at TEXT NOT NULL,
    prior_cash_balance REAL NOT NULL,
    prior_realized_pnl REAL NOT NULL,
    prior_peak_equity REAL NOT NULL,
    prior_max_drawdown REAL NOT NULL,
    reset_equity REAL NOT NULL,
    preserved_closed_trades INTEGER NOT NULL,
    preserved_net_pnl REAL NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_strategy_state (
    strategy_id TEXT PRIMARY KEY,
    armed INTEGER NOT NULL DEFAULT 1,
    last_completion REAL NOT NULL DEFAULT 0,
    last_processed_bar TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    expires_at TEXT,
    queued_at TEXT NOT NULL,
    planned_entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    quantity INTEGER NOT NULL,
    completion_ratio REAL NOT NULL,
    fill_price REAL,
    filled_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    initial_risk_dollars REAL NOT NULL,
    actual_quantity INTEGER,
    actual_risk_dollars REAL,
    actual_reward_risk REAL,
    context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES paper_orders(order_id),
    signal_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    planned_entry REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl REAL,
    commission REAL NOT NULL DEFAULT 0,
    slippage_cost REAL NOT NULL DEFAULT 0,
    net_pnl REAL,
    result_points REAL,
    result_r REAL,
    mae_points REAL NOT NULL DEFAULT 0,
    mfe_points REAL NOT NULL DEFAULT 0,
    time_to_mae_bars INTEGER,
    time_to_mfe_bars INTEGER,
    bars_held INTEGER NOT NULL DEFAULT 0,
    highest_price REAL NOT NULL,
    lowest_price REAL NOT NULL,
    entry_completion REAL NOT NULL,
    initial_risk_dollars REAL NOT NULL,
    context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_stop_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
    timestamp TEXT NOT NULL,
    prior_stop REAL NOT NULL,
    new_stop REAL NOT NULL,
    reference_price REAL NOT NULL,
    distance_points REAL NOT NULL,
    entry_atr REAL,
    initial_risk_points REAL NOT NULL,
    mfe_r REAL NOT NULL,
    policy_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    context_json TEXT NOT NULL
);
-- This is intentionally an outbox, not an order queue.  It contains immutable
-- observations of the local paper lifecycle and has no broker/account fields.
CREATE TABLE IF NOT EXISTS execution_intents (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'ENTRY_READY','ENTRY_CANCELLED','PAPER_FILLED','STOP_REPLACE','EXIT_OBSERVED'
    )),
    strategy_id TEXT NOT NULL CHECK(strategy_id='failed_break_reclaim'),
    strategy_version TEXT NOT NULL,
    candidate_id TEXT,
    order_id TEXT,
    position_id TEXT,
    symbol TEXT,
    direction TEXT,
    quantity INTEGER,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    expires_at TEXT,
    effective_after TEXT,
    policy_version TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS execution_intents_no_update
BEFORE UPDATE ON execution_intents
BEGIN
    SELECT RAISE(ABORT, 'execution intents are append-only');
END;
CREATE TRIGGER IF NOT EXISTS execution_intents_no_delete
BEFORE DELETE ON execution_intents
BEGIN
    SELECT RAISE(ABORT, 'execution intents are append-only');
END;
CREATE TABLE IF NOT EXISTS paper_trade_replay_bars (
    position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    phase TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(position_id, timestamp)
);
CREATE TABLE IF NOT EXISTS paper_equity (
    equity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL UNIQUE,
    cash_balance REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    equity REAL NOT NULL,
    open_risk REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    daily_realized_pnl REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_post_exit_bars (
    position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
    bar_number INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    directional_return_from_exit REAL NOT NULL,
    directional_return_from_entry REAL NOT NULL,
    cumulative_favorable_points REAL NOT NULL,
    cumulative_adverse_points REAL NOT NULL,
    recovered_entry INTEGER NOT NULL DEFAULT 0,
    continued_beyond_target INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(position_id, bar_number),
    UNIQUE(position_id, timestamp)
);
CREATE TABLE IF NOT EXISTS paper_fee_policy_changes (
    policy_key TEXT PRIMARY KEY,
    changed_at TEXT NOT NULL,
    prior_policy TEXT NOT NULL,
    new_policy TEXT NOT NULL,
    affected_trades INTEGER NOT NULL,
    recovered_commission REAL NOT NULL,
    recovered_slippage REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_ai_decisions (
    decision_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    candidate_id TEXT NOT NULL UNIQUE,
    decision TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    expected_r REAL,
    probability_positive REAL,
    similar_trades INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    exploration INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    feature_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_feature_registry (
    feature_version_id TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    missing_policy TEXT NOT NULL,
    standardization_rule TEXT NOT NULL,
    explanation_label TEXT NOT NULL,
    feature_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(feature_version_id,feature_name)
);
CREATE TABLE IF NOT EXISTS paper_model_fits (
    model_version_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    training_cutoff TEXT NOT NULL,
    training_rows INTEGER NOT NULL,
    realized_rows INTEGER NOT NULL,
    replayed_rows INTEGER NOT NULL,
    feature_version_id TEXT NOT NULL,
    policy_version_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    training_data_hash TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_posterior_snapshots (
    posterior_snapshot_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model_version_id TEXT NOT NULL REFERENCES paper_model_fits(model_version_id),
    strategy_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    posterior_mean_expected_r REAL NOT NULL,
    posterior_sd_expected_r REAL NOT NULL,
    prob_expected_r_gt_0 REAL NOT NULL,
    prob_expected_r_gt_threshold REAL NOT NULL,
    prob_expected_r_lt_severe_negative REAL NOT NULL,
    lower_credible_bound_expected_r REAL NOT NULL,
    predictive_q10_r REAL NOT NULL,
    predictive_expected_shortfall_r REAL,
    local_effective_sample_size REAL NOT NULL,
    novelty_score REAL NOT NULL,
    coverage_gap_score REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_decision_audit_v2 (
    audit_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_family_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    parent_version_id TEXT,
    epoch_id TEXT NOT NULL,
    model_version_id TEXT,
    policy_version_id TEXT NOT NULL,
    feature_version_id TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    feature_snapshot_hash TEXT NOT NULL,
    decision_seed TEXT,
    data_freshness_status TEXT NOT NULL,
    bar_close_status TEXT NOT NULL,
    hard_pass_all INTEGER NOT NULL,
    hard_rules_json TEXT NOT NULL,
    hard_failure_reason_codes_json TEXT NOT NULL,
    eligible_for_decision INTEGER NOT NULL,
    eligible_for_exploration INTEGER NOT NULL,
    chosen_action TEXT NOT NULL,
    action_prob_take REAL,
    action_prob_skip REAL,
    is_exploratory INTEGER NOT NULL,
    decision_reason_code TEXT NOT NULL,
    soft_reason_summary TEXT NOT NULL,
    coverage_bucket_id TEXT NOT NULL,
    session_segment TEXT NOT NULL,
    direction TEXT NOT NULL,
    posterior_snapshot_id TEXT REFERENCES paper_posterior_snapshots(posterior_snapshot_id),
    sampled_posterior_value_r REAL,
    exploration_score REAL,
    unclipped_probability REAL,
    clipped_probability REAL,
    random_draw REAL,
    decision_role TEXT NOT NULL,
    controls_paper_execution INTEGER NOT NULL,
    outcome_available INTEGER NOT NULL DEFAULT 0,
    outcome_source TEXT NOT NULL DEFAULT 'unavailable',
    realized_r REAL,
    entry_time TEXT,
    exit_time TEXT,
    exit_reason TEXT,
    win_flag INTEGER,
    mae_r REAL,
    mfe_r REAL,
    stop_hit_first INTEGER,
    target_hit_first INTEGER,
    bars_held INTEGER,
    simulator_version TEXT,
    training_inclusion_flag INTEGER NOT NULL DEFAULT 0,
    feature_json TEXT NOT NULL,
    policy_state_json TEXT NOT NULL,
    audit_hash TEXT NOT NULL,
    UNIQUE(candidate_id,decision_role)
);
CREATE TABLE IF NOT EXISTS paper_rejected_setup_replays (
    replay_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    audit_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    opened_at TEXT,
    closed_at TEXT,
    direction TEXT NOT NULL,
    session_segment TEXT NOT NULL,
    decision_reason_code TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    status TEXT NOT NULL,
    planned_entry REAL NOT NULL,
    fill_price REAL,
    initial_stop REAL NOT NULL,
    current_stop REAL NOT NULL,
    target_price REAL NOT NULL,
    highest_price REAL,
    lowest_price REAL,
    bars_held INTEGER NOT NULL DEFAULT 0,
    mae_r REAL NOT NULL DEFAULT 0,
    mfe_r REAL NOT NULL DEFAULT 0,
    realized_r REAL,
    exit_price REAL,
    exit_reason TEXT,
    outcome_class TEXT,
    trailing_active INTEGER NOT NULL DEFAULT 0,
    simulator_version TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_strategy_time ON strategy_events(strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_state_time ON strategy_events(state_to, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_strategy_time ON signals(strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_signal ON decisions(signal_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_open ON trades(strategy_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs(strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_strategy_evolution_time ON strategy_evolution(strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_evolution_analysis ON strategy_evolution(analysis_id, created_at);
CREATE INDEX IF NOT EXISTS idx_strategy_change_epochs_time ON strategy_change_epochs(strategy_id, activated_at);
CREATE INDEX IF NOT EXISTS idx_evolution_automation_time ON evolution_automation_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_evolution_hypotheses_strategy_time ON evolution_hypotheses(strategy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_evidence_version_time ON strategy_evidence_snapshots(strategy_id, strategy_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_weakness_snapshot_rank ON strategy_weaknesses(evidence_snapshot_id, rank_order);
CREATE INDEX IF NOT EXISTS idx_strategy_hypothesis_signature ON strategy_hypotheses_v2(strategy_id, normalized_signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_experiment_status ON strategy_experiments(strategy_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_opportunity_experiment_time ON strategy_experiment_opportunities(experiment_id, market_time);
CREATE INDEX IF NOT EXISTS idx_strategy_experiment_evidence_time ON strategy_experiment_evidence_snapshots(experiment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_shadow_experiment_role ON strategy_shadow_positions(experiment_id, role, status, signal_time);
CREATE INDEX IF NOT EXISTS idx_strategy_evolution_audit_time ON strategy_evolution_audit(strategy_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_strategy_evidence_invalidation_target
ON strategy_evidence_invalidations(target_type, target_id, invalidated_at);
CREATE INDEX IF NOT EXISTS idx_strategy_version_lineage_strategy
ON strategy_version_lineage(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notes_entity ON notes(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_paper_orders_status_time ON paper_orders(status, signal_time);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status_time ON paper_positions(status, opened_at);
CREATE INDEX IF NOT EXISTS idx_paper_positions_strategy_time ON paper_positions(strategy_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_strategy_retirements_account ON paper_strategy_retirements(account_id, retired_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_account_resets_account ON paper_account_resets(account_id, reset_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_stop_adjustments_position_time
ON paper_stop_adjustments(position_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_execution_intents_fbr_sequence
ON execution_intents(strategy_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_intents_one_lifecycle_event
ON execution_intents(order_id, event_type)
WHERE event_type IN ('ENTRY_READY','ENTRY_CANCELLED','PAPER_FILLED','EXIT_OBSERVED');
CREATE INDEX IF NOT EXISTS idx_paper_trade_replay_position_time
ON paper_trade_replay_bars(position_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_equity_time ON paper_equity(timestamp);
CREATE INDEX IF NOT EXISTS idx_paper_post_exit_time ON paper_post_exit_bars(position_id, bar_number);
CREATE INDEX IF NOT EXISTS idx_paper_ai_decisions_time ON paper_ai_decisions(strategy_id, bar_time DESC);
CREATE INDEX IF NOT EXISTS idx_paper_model_fits_family_time ON paper_model_fits(family_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_posterior_candidate ON paper_posterior_snapshots(candidate_id);
CREATE INDEX IF NOT EXISTS idx_paper_decision_v2_strategy_time ON paper_decision_audit_v2(strategy_id,timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_paper_decision_v2_version_time ON paper_decision_audit_v2(strategy_version_id,timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_paper_decision_v2_coverage ON paper_decision_audit_v2(coverage_bucket_id,timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_paper_decision_v2_role_outcome ON paper_decision_audit_v2(decision_role,outcome_available,timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_rejected_replay_strategy_status ON paper_rejected_setup_replays(strategy_id,status,signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_rejected_replay_outcome ON paper_rejected_setup_replays(outcome_class,closed_at DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            state_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(strategy_evolution_state)"
                ).fetchall()
            }
            if "last_learning_cycle_trade_count" not in state_columns:
                connection.execute(
                    "ALTER TABLE strategy_evolution_state ADD COLUMN "
                    "last_learning_cycle_trade_count INTEGER NOT NULL DEFAULT 0"
                )
            fee_policy_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(paper_fee_policy_changes)"
                ).fetchall()
            }
            if "recovered_slippage" not in fee_policy_columns:
                connection.execute(
                    "ALTER TABLE paper_fee_policy_changes ADD COLUMN "
                    "recovered_slippage REAL NOT NULL DEFAULT 0"
                )
            order_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(paper_orders)"
                ).fetchall()
            }
            for name, declaration in (
                ("expires_at", "TEXT"),
                ("actual_quantity", "INTEGER"),
                ("actual_risk_dollars", "REAL"),
                ("actual_reward_risk", "REAL"),
            ):
                if name not in order_columns:
                    connection.execute(
                        f"ALTER TABLE paper_orders ADD COLUMN {name} {declaration}"
                    )
            # Existing order history is immutable. Expiration was already stored
            # on the linked signal, so migration 15 copies that exact value rather
            # than deriving or inventing a new lifetime.
            connection.execute(
                """UPDATE paper_orders SET expires_at=(
                    SELECT signals.expires_at FROM signals
                    WHERE signals.signal_id=paper_orders.candidate_id
                ) WHERE expires_at IS NULL"""
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )

    def rollback_strategy_evolution_schema(self) -> None:
        """Remove only the V2 evolution subsystem; preserve the legacy journal.

        This deliberately has no API route. It exists for controlled operator
        rollback and migration verification, never for the running agent.
        """
        tables = (
            "strategy_version_lineage",
            "strategy_evidence_invalidations",
            "strategy_experiment_evidence_snapshots",
            "strategy_shadow_positions",
            "strategy_experiment_opportunities",
            "strategy_experiments",
            "strategy_challengers",
            "strategy_hypotheses_v2",
            "strategy_weaknesses",
            "strategy_evidence_snapshots",
            "strategy_evolution_state",
            "strategy_evolution_audit",
        )
        with self.connection() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version>=7")
            connection.execute("PRAGMA foreign_keys=ON")

    def rollback_adaptive_decision_schema(self) -> None:
        """Remove only Bayesian-bandit records; preserve every legacy decision."""
        tables = (
            "paper_decision_audit_v2",
            "paper_posterior_snapshots",
            "paper_model_fits",
            "paper_feature_registry",
            "paper_policy_versions",
        )
        with self.connection() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version>=9")
            connection.execute("PRAGMA foreign_keys=ON")

    def register_strategy_version(self, config: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO strategy_versions
                (version_hash, strategy_id, semantic_version, config_json, evidence_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    config["version_hash"], config["strategy_id"], config.get("version", "v1"),
                    _json(config), config.get("promotion_status", "HYPOTHESIS_DEFAULT"),
                    config.get("created_date", datetime.now(UTC).date().isoformat()),
                ),
            )

    def record_evaluation(
        self,
        evaluation: StrategyEvaluation,
        *,
        prior_state: str | None,
        mode: str,
        data_quality: dict[str, Any],
    ) -> str | None:
        if prior_state == evaluation.state.value and evaluation.state.value not in {"READY", "BLOCKED", "STALE_DATA"}:
            return None
        event_id = str(uuid4())
        ticket = evaluation.ticket
        reason = evaluation.explanation or "; ".join(evaluation.missing_conditions)
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO strategy_events
                (event_id,timestamp,strategy_id,strategy_version,symbol,mode,state_from,state_to,reason,
                 data_source,data_freshness_json,checklist_json,feature_json,reference_level_json,signal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, evaluation.timestamp.isoformat(), evaluation.strategy_id,
                    evaluation.strategy_version, ticket.symbol if ticket else data_quality.get("active_symbol", "UNKNOWN"),
                    mode, prior_state, evaluation.state.value, reason, data_quality.get("source", "UNKNOWN"),
                    _json(data_quality), _json([item.as_dict() for item in evaluation.conditions]),
                    _json(evaluation.relevant_features), _json(evaluation.reference_level.as_dict()) if evaluation.reference_level else None,
                    ticket.signal_id if ticket else None,
                ),
            )
            if ticket:
                self._insert_signal(connection, evaluation.strategy_id, mode, ticket)
        return event_id

    @staticmethod
    def _insert_signal(connection: sqlite3.Connection, strategy_id: str, mode: str, ticket: TradeTicket) -> None:
        payload = ticket.as_dict()
        connection.execute(
            """INSERT OR IGNORE INTO signals
            (signal_id,timestamp,strategy_id,strategy_version,symbol,mode,direction,entry,stop,target,quantity,expires_at,ticket_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ticket.signal_id, ticket.timestamp.isoformat(), strategy_id, ticket.strategy_version,
                ticket.symbol, mode, ticket.direction.value, ticket.entry, ticket.stop, ticket.target,
                ticket.quantity, ticket.expiration_time.isoformat(), _json(payload),
            ),
        )

    def attach_snapshot(self, signal_id: str, event_id: str | None, path: str, features: dict[str, Any]) -> str:
        snapshot_id = str(uuid4())
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO snapshots(snapshot_id,signal_id,event_id,timestamp,path,feature_json) VALUES(?,?,?,?,?,?)",
                (snapshot_id, signal_id, event_id, datetime.now(UTC).isoformat(), path, _json(features)),
            )
            connection.execute("UPDATE signals SET snapshot_path=? WHERE signal_id=?", (path, signal_id))
        return snapshot_id

    def record_decision(self, signal_id: str, decision: str, reason: str = "", notes: str = "") -> str:
        if decision not in {"TAKE", "SKIP", "EXPIRED", "CANCELLED"}:
            raise ValueError("Unsupported decision")
        decision_id = str(uuid4())
        with self.connection() as connection:
            exists = connection.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if not exists:
                raise KeyError(f"Unknown signal {signal_id}")
            connection.execute(
                "INSERT INTO decisions(decision_id,signal_id,timestamp,decision,reason_code,notes) VALUES(?,?,?,?,?,?)",
                (decision_id, signal_id, datetime.now(UTC).isoformat(), decision, reason, notes),
            )
        return decision_id

    def record_execution(
        self, signal_id: str, fill_price: float, quantity: int, *, mode: str = "MANUAL", commission: float = 0.0, slippage: float = 0.0
    ) -> str:
        execution_id = str(uuid4())
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO executions(execution_id,signal_id,timestamp,mode,fill_price,quantity,commission,slippage) VALUES(?,?,?,?,?,?,?,?)",
                (execution_id, signal_id, datetime.now(UTC).isoformat(), mode, fill_price, quantity, commission, slippage),
            )
        return execution_id

    def record_completed_trade(self, signal_id: str, outcome: dict[str, Any]) -> str:
        trade_id = str(uuid4())
        with self.connection() as connection:
            signal = connection.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if not signal:
                raise KeyError(signal_id)
            connection.execute(
                """INSERT INTO trades
                (trade_id,signal_id,strategy_id,strategy_version,opened_at,closed_at,entry,exit,stop,target,
                 quantity,direction,result_points,result_r,result_dollars,mae,mfe,time_to_mae_bars,
                 time_to_mfe_bars,exit_reason,context_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade_id, signal_id, signal["strategy_id"], signal["strategy_version"],
                    signal["timestamp"], outcome.get("closed_at", datetime.now(UTC).isoformat()),
                    signal["entry"], outcome["exit"], signal["stop"], signal["target"], signal["quantity"],
                    signal["direction"], outcome.get("result_points"), outcome.get("result_r"),
                    outcome.get("result_dollars"), outcome.get("mae"), outcome.get("mfe"),
                    outcome.get("time_to_mae_bars"), outcome.get("time_to_mfe_bars"),
                    outcome.get("exit_reason", "SIMULATED"), _json(outcome.get("context", {})),
                ),
            )
        return trade_id


    def add_note(self, entity_type: str, entity_id: str, note: str, tags: list[str] | None = None) -> str:
        note_id = str(uuid4())
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO notes(note_id,entity_type,entity_id,timestamp,note,tags_json) VALUES(?,?,?,?,?,?)",
                (note_id, entity_type, entity_id, datetime.now(UTC).isoformat(), note, _json(tags or [])),
            )
        return note_id

    def list_events(
        self,
        limit: int = 200,
        strategy_id: str | None = None,
        strategy_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM strategy_events"
        params: list[Any] = []
        filters: list[str] = []
        if strategy_id:
            filters.append("strategy_id=?")
            params.append(strategy_id)
        if strategy_ids is not None:
            values = sorted(str(item) for item in strategy_ids)
            if not values:
                return []
            marks = ",".join("?" for _ in values)
            filters.append(f"strategy_id IN ({marks})")
            params.extend(values)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    @staticmethod
    def record_execution_intent(
        connection: sqlite3.Connection,
        *,
        observed_at: str,
        event_type: str,
        strategy_id: str,
        strategy_version: str,
        payload: dict[str, Any],
        candidate_id: str | None = None,
        order_id: str | None = None,
        position_id: str | None = None,
        symbol: str | None = None,
        direction: str | None = None,
        quantity: int | None = None,
        entry_price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        expires_at: str | None = None,
        effective_after: str | None = None,
        policy_version: str | None = None,
    ) -> str | None:
        """Append one FBR paper-lifecycle observation to the read-only outbox.

        The caller supplies its existing transaction connection, making this
        record atomic with the paper state change.  This method deliberately
        has no transport, broker, account, credential, acknowledgement, or
        control behaviour.
        """
        allowed = {
            "ENTRY_READY", "ENTRY_CANCELLED", "PAPER_FILLED", "STOP_REPLACE",
            "EXIT_OBSERVED",
        }
        if event_type not in allowed:
            raise ValueError("Unsupported execution intent event")
        if strategy_id != "failed_break_reclaim":
            return None
        payload_json = _json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event_id = str(uuid4())
        cursor = connection.execute(
            """INSERT OR IGNORE INTO execution_intents
            (event_id,observed_at,event_type,strategy_id,strategy_version,candidate_id,
             order_id,position_id,symbol,direction,quantity,entry_price,stop_price,target_price,
             expires_at,effective_after,policy_version,payload_json,payload_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, observed_at, event_type, strategy_id, strategy_version,
                candidate_id, order_id, position_id, symbol, direction, quantity,
                entry_price, stop_price, target_price, expires_at, effective_after,
                policy_version, payload_json, payload_hash,
            ),
        )
        return event_id if cursor.rowcount else None

    def list_execution_intents_fbr(
        self, *, after: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return bounded, cursor-ordered immutable FBR observations only."""
        after = max(0, int(after))
        limit = min(max(1, int(limit)), 500)
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT sequence,event_id,observed_at,event_type,strategy_id,
                strategy_version,candidate_id,order_id,position_id,symbol,direction,
                quantity,entry_price,stop_price,target_price,expires_at,effective_after,
                policy_version,payload_json,payload_hash
                FROM execution_intents
                WHERE strategy_id='failed_break_reclaim' AND sequence>?
                ORDER BY sequence ASC LIMIT ?""",
                (after, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def list_signals(
        self, limit: int = 100, strategy_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        strategy_ids = None if strategy_ids is None else {str(item) for item in strategy_ids}
        if strategy_ids is not None and not strategy_ids:
            return []
        strategy_filter = ""
        params: list[Any] = []
        if strategy_ids is not None:
            marks = ",".join("?" for _ in strategy_ids)
            strategy_filter = f" WHERE s.strategy_id IN ({marks})"
            params.extend(sorted(strategy_ids))
        params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT s.*, d.decision, d.notes FROM signals s
                LEFT JOIN decisions d ON d.decision_id=(SELECT decision_id FROM decisions WHERE signal_id=s.signal_id ORDER BY timestamp DESC LIMIT 1)
                """ + strategy_filter + " ORDER BY s.timestamp DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_backtest_runs(
        self, limit: int = 100, strategy_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        strategy_ids = None if strategy_ids is None else {str(item) for item in strategy_ids}
        if strategy_ids is not None and not strategy_ids:
            return []
        strategy_filter = ""
        params: list[Any] = []
        if strategy_ids is not None:
            marks = ",".join("?" for _ in strategy_ids)
            strategy_filter = f" WHERE strategy_id IN ({marks})"
            params.extend(sorted(strategy_ids))
        params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM backtest_runs" + strategy_filter
                + " ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metrics"] = json.loads(item.pop("metrics_json"))
                item["split_metrics"] = json.loads(item.pop("split_metrics_json"))
                result.append(item)
            return result

    def record_backtest_run(
        self,
        run: dict[str, Any],
        trades: list[dict[str, Any]],
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO backtest_runs
                (run_id,created_at,strategy_id,strategy_version,date_start,date_end,mode,parameter_json,
                 data_fingerprint,metrics_json,split_metrics_json,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["run_id"], run["created_at"], run["strategy_id"], run["strategy_version"],
                    run.get("date_start"), run.get("date_end"), run.get("mode", "BACKTEST"),
                    _json(run.get("parameters", {})), run["data_fingerprint"], _json(run["metrics"]),
                    _json(run.get("split_metrics", {})), run.get("status", "COMPLETED"),
                ),
            )
            connection.execute("DELETE FROM backtest_trades WHERE run_id=?", (run["run_id"],))
            for trade in trades:
                connection.execute(
                    """INSERT INTO backtest_trades
                    (backtest_trade_id,run_id,signal_id,strategy_id,strategy_version,timestamp,entry,exit,
                     stop,target,direction,result_points,result_r,result_dollars,mae,mfe,time_to_mae_bars,
                     time_to_mfe_bars,bars_held,first_hit,forward_returns_json,feature_json,parameter_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trade["backtest_trade_id"], run["run_id"], trade["signal_id"], trade["strategy_id"],
                        trade["strategy_version"], trade["timestamp"], trade["entry"], trade["exit"],
                        trade["stop"], trade["target"], trade["direction"], trade["result_points"],
                        trade["result_r"], trade["result_dollars"], trade["mae"], trade["mfe"],
                        trade["time_to_mae_bars"], trade["time_to_mfe_bars"], trade["bars_held"],
                        trade["first_hit"], _json(trade["forward_returns"]), _json(trade["features"]),
                        _json(trade.get("parameters", {})),
                    ),
                )

    def backtest_trades(
        self,
        run_id: str,
        limit: int = 5000,
        strategy_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        strategy_ids = None if strategy_ids is None else {str(item) for item in strategy_ids}
        if strategy_ids is not None and not strategy_ids:
            return []
        strategy_filter = ""
        params: list[Any] = [run_id]
        if strategy_ids is not None:
            marks = ",".join("?" for _ in strategy_ids)
            strategy_filter = f" AND strategy_id IN ({marks})"
            params.extend(sorted(strategy_ids))
        params.append(limit)
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM backtest_trades WHERE run_id=?" + strategy_filter
                    + " ORDER BY timestamp LIMIT ?",
                    params,
                ).fetchall()
            ]

    def record_evolution(self, record: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO strategy_evolution
                (evolution_id,analysis_id,created_at,strategy_id,champion_version,challenger_version,
                 changed_parameter,old_value_json,new_value_json,reason,decision,baseline_run_id,
                 challenger_run_id,evidence_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["evolution_id"], record["analysis_id"], record["created_at"],
                    record["strategy_id"], record["champion_version"], record["challenger_version"],
                    record["changed_parameter"], _json(record.get("old_value")),
                    _json(record.get("new_value")), record["reason"], record["decision"],
                    record["baseline_run_id"], record["challenger_run_id"],
                    _json(record.get("evidence", {})),
                ),
            )

    def list_evolution(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_evolution ORDER BY created_at DESC, strategy_id LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["old_value"] = json.loads(item.pop("old_value_json"))
            item["new_value"] = json.loads(item.pop("new_value_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def register_strategy_epoch(self, strategy_id: str, to_version: str, change: dict[str, Any]) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO strategy_change_epochs
                (epoch_id,strategy_id,from_version,to_version,activated_at,summary,reasoning,
                 changes_json,evidence_json,status)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(change["change_id"]), strategy_id, str(change["from_version"]), to_version,
                    str(change["activated_at"]), str(change["summary"]), str(change["reason"]),
                    _json(change.get("changes", [])), _json(change.get("evidence", {})),
                    str(change.get("status", "ACTIVE_PAPER_TEST")),
                ),
            )
            return cursor.rowcount > 0

    def list_strategy_epochs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_change_epochs ORDER BY activated_at, strategy_id LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["changes"] = json.loads(item.pop("changes_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result

    def record_automation_run(self, record: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO evolution_automation_runs
                (automation_id,analysis_id,started_at,completed_at,status,trigger_name,data_start,
                 data_end,bars,sessions,activated_count,reviews_json,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["automation_id"], record.get("analysis_id"), record["started_at"],
                    record.get("completed_at"), record["status"], record["trigger_name"],
                    record.get("data_start"), record.get("data_end"), int(record.get("bars", 0)),
                    int(record.get("sessions", 0)), int(record.get("activated_count", 0)),
                    _json(record.get("reviews", [])), record.get("error"),
                ),
            )

    def list_automation_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evolution_automation_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reviews"] = json.loads(item.pop("reviews_json"))
            result.append(item)
        return result

    def upsert_evolution_agent_state(self, state: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO evolution_agent_state
                (strategy_id,phase,current_version,active_hypothesis_id,last_trade_id,
                 last_trade_count,next_review_trade_count,details_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                  phase=excluded.phase,current_version=excluded.current_version,
                  active_hypothesis_id=excluded.active_hypothesis_id,
                  last_trade_id=excluded.last_trade_id,last_trade_count=excluded.last_trade_count,
                  next_review_trade_count=excluded.next_review_trade_count,
                  details_json=excluded.details_json,updated_at=excluded.updated_at""",
                (
                    state["strategy_id"], state["phase"], state["current_version"],
                    state.get("active_hypothesis_id"), state.get("last_trade_id"),
                    int(state.get("last_trade_count", 0)),
                    int(state.get("next_review_trade_count", 0)),
                    _json(state.get("details", {})), state.get("updated_at", datetime.now(UTC).isoformat()),
                ),
            )

    def evolution_agent_states(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evolution_agent_state ORDER BY strategy_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def evolution_agent_state(self, strategy_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.evolution_agent_states() if item["strategy_id"] == strategy_id),
            None,
        )

    def record_evolution_hypothesis(self, hypothesis: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO evolution_hypotheses
                (hypothesis_id,strategy_id,created_at,updated_at,status,analysis_id,epoch_id,
                 baseline_version,candidate_version,parameter,old_value_json,new_value_json,
                 baseline_target,experiment_target,baseline_metrics_json,experiment_metrics_json,
                 rationale,decision_reason,concluded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    hypothesis["hypothesis_id"], hypothesis["strategy_id"],
                    hypothesis.get("created_at", now), hypothesis.get("updated_at", now),
                    hypothesis["status"], hypothesis.get("analysis_id"), hypothesis.get("epoch_id"),
                    hypothesis["baseline_version"], hypothesis.get("candidate_version"),
                    hypothesis.get("parameter"), _json(hypothesis.get("old_value")),
                    _json(hypothesis.get("new_value")), int(hypothesis.get("baseline_target", 30)),
                    int(hypothesis.get("experiment_target", 30)),
                    _json(hypothesis.get("baseline_metrics", {})),
                    _json(hypothesis.get("experiment_metrics", {})), hypothesis["rationale"],
                    hypothesis.get("decision_reason"), hypothesis.get("concluded_at"),
                ),
            )

    def list_evolution_hypotheses(
        self, limit: int = 100, strategy_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM evolution_hypotheses"
        params: list[Any] = []
        if strategy_id:
            query += " WHERE strategy_id=?"
            params.append(strategy_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field in ("old_value", "new_value", "baseline_metrics", "experiment_metrics"):
                item[field] = json.loads(item.pop(f"{field}_json"))
            result.append(item)
        return result

    def update_strategy_epoch_status(
        self, epoch_id: str, status: str, conclusion: dict[str, Any]
    ) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM strategy_change_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            if not row:
                return
            evidence = json.loads(row["evidence_json"])
            evidence["trade_driven_conclusion"] = conclusion
            connection.execute(
                "UPDATE strategy_change_epochs SET status=?,evidence_json=? WHERE epoch_id=?",
                (status, _json(evidence), epoch_id),
            )

    def promote_version(self, version_hash: str) -> None:
        with self.connection() as connection:
            row = connection.execute("SELECT strategy_id FROM strategy_versions WHERE version_hash=?", (version_hash,)).fetchone()
            if not row:
                raise KeyError(version_hash)
            connection.execute("UPDATE strategy_versions SET is_live=0 WHERE strategy_id=?", (row["strategy_id"],))
            connection.execute(
                "UPDATE strategy_versions SET is_live=1,evidence_status='PROMOTED',promoted_at=? WHERE version_hash=?",
                (datetime.now(UTC).isoformat(), version_hash),
            )

    def versions(self, strategy_ids: set[str] | None = None) -> list[dict[str, Any]]:
        strategy_ids = None if strategy_ids is None else {str(item) for item in strategy_ids}
        if strategy_ids is not None and not strategy_ids:
            return []
        strategy_filter = ""
        params: list[Any] = []
        if strategy_ids is not None:
            marks = ",".join("?" for _ in strategy_ids)
            strategy_filter = f" WHERE strategy_id IN ({marks})"
            params.extend(sorted(strategy_ids))
        with self.connection() as connection:
            return [
                dict(row) for row in connection.execute(
                    "SELECT * FROM strategy_versions" + strategy_filter
                    + " ORDER BY strategy_id, created_at DESC", params
                ).fetchall()
            ]


def create_chart_snapshot(
    directory: str | Path,
    signal_id: str,
    strategy_id: str,
    bars: list[Bar],
    ticket: TradeTicket,
) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{signal_id}.svg"
    shown = bars[-50:]
    width, height, pad = 900, 420, 42
    low = min([bar.low for bar in shown] + [ticket.stop, ticket.target])
    high = max([bar.high for bar in shown] + [ticket.stop, ticket.target])
    span = high - low or 1.0

    def y(price: float) -> float:
        return pad + (high - price) / span * (height - pad * 2)

    step = (width - pad * 2) / max(1, len(shown))
    candles = []
    for index, bar in enumerate(shown):
        x = pad + index * step + step / 2
        color = "#36d399" if bar.close >= bar.open else "#fb7185"
        top, bottom = y(max(bar.open, bar.close)), y(min(bar.open, bar.close))
        candles.append(f'<line x1="{x:.1f}" y1="{y(bar.high):.1f}" x2="{x:.1f}" y2="{y(bar.low):.1f}" stroke="{color}"/>')
        candles.append(f'<rect x="{x-step*0.28:.1f}" y="{top:.1f}" width="{step*0.56:.1f}" height="{max(1,bottom-top):.1f}" fill="{color}"/>')
    lines = []
    for label, value, color in (("ENTRY", ticket.entry, "#e8edf7"), ("STOP", ticket.stop, "#fb7185"), ("TARGET", ticket.target, "#36d399")):
        lines.append(f'<line x1="{pad}" y1="{y(value):.1f}" x2="{width-pad}" y2="{y(value):.1f}" stroke="{color}" stroke-dasharray="6 5"/>')
        lines.append(f'<text x="{width-pad+4}" y="{y(value)+4:.1f}" fill="{color}" font-size="11">{label}</text>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#08111f"/>'
        f'<text x="{pad}" y="25" fill="#d9e5f5" font-family="sans-serif" font-size="15">SEB V1 · {strategy_id} · {ticket.direction.value}</text>'
        + "".join(candles + lines)
        + '</svg>'
    )
    path.write_text(svg, encoding="utf-8")
    return path
