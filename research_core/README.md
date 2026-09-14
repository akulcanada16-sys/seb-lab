# Offline research runner

This folder runs a reproducible, offline MNQ research example. It does not connect to a broker, live market feed, account, or database.

From the project folder, create a private Python environment and install the small timezone-data dependency once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r research_core\requirements.txt
```

Create the supplied seeded example result:

```powershell
.\.venv\Scripts\python.exe research_core\cli.py --output research_core\sample-result.json
```

Run a JSON input file with a `bars` array or CSV text payload:

```powershell
.\.venv\Scripts\python.exe research_core\cli.py my-input.json --output my-result.json
```

Use `public_api.run_research()` or the CLI for public results. `seb_engine` is an internal copied engine; its legacy internal metrics are deliberately not the public reporting contract.
