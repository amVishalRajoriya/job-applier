@echo off
rem One-step start on Windows: installs what is needed the first time, then opens the dashboard.
cd /d "%~dp0"
if not exist .venv (
  echo First run: creating a virtual environment and installing dependencies (a few minutes)...
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
if not exist .venv\.installed (
  pip install -q -r requirements.txt || exit /b 1
  python -m playwright install chromium || exit /b 1
  type nul > .venv\.installed
)
if not exist .env (
  copy .env.example .env >nul
  for /f %%k in ('python -m jobagent.cli keygen') do python -c "import pathlib,sys; p=pathlib.Path('.env'); p.write_text(p.read_text().replace('JOBAGENT_ENCRYPTION_KEY=\n','JOBAGENT_ENCRYPTION_KEY='+sys.argv[1]+'\n'))" %%k
  echo Created .env with a new encryption key. Back it up: without it the saved profile cannot be read.
)
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%b"=="" set "%%a=%%b"
python -m jobagent.cli init >nul
python -m jobagent.cli dashboard --open
