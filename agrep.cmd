@echo off
setlocal DisableDelayedExpansion
set AGREP_CLI_NAME=agrep
rem %* preserves the caller's original quoting. Rebuilding argv through a batch
rem variable strips quote boundaries and can expose legal &, ^, %, ! characters
rem to a second cmd.exe parse.
python "%~dp0cli.py" %*
exit /b %ERRORLEVEL%
