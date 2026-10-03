@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First create the environment: python -m venv .venv
  echo Then install: .venv\Scripts\python.exe -m pip install -r requirements-gui.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import flask, embreex, manifold3d, trimesh, cv2, PIL, shapely" >nul 2>&1
if errorlevel 1 (
  echo Install the app dependencies first:
  echo .venv\Scripts\python.exe -m pip install -r requirements-gui.txt
  pause
  exit /b 1
)
".venv\Scripts\python.exe" gui.py
if errorlevel 1 pause
