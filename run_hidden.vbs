' Запускает уведомления Ozon в фоне без окна консоли.
' pythonw.exe берётся из PATH, рабочий каталог — папка рядом со скриптом.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = baseDir
sh.Run "pythonw.exe """ & baseDir & "\ozon_alert.py""", 0, False
