@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Слежу за новыми заказами Ozon. Это окно можно свернуть, но не закрывать.
echo (Для тихого фона при загрузке ПК используется автозапуск, см. README.)
python ozon_alert.py
pause
