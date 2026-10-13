@echo off
rem Run the widget without a console window
where pythonw >/dev/null 2>/dev/null && (start "" pythonw "%~dp0limits_widget.py") || (start "" pyw "%~dp0limits_widget.py")
