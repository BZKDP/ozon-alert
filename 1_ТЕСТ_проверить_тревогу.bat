@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Показываю демо-окно нового Express-заказа (должно заорать и всплыть).
echo Нажми "ПРИНЯЛ" чтобы закрыть.
python ozon_alert.py --test
pause
