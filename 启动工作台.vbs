Option Explicit
' ============================================================
'  启动工作台 —— 一次双击搞定
'
'  它做四件事：
'    1. 关掉占着端口的旧进程。不关的话：新代码不生效（Flask 把模板缓存在内存里），
'       而且新进程会因为端口被占直接退出。
'    2. 无窗口启动工作台，所有输出写进 workbench.log
'    3. 等它真的起来了才开浏览器（而不是开个白页让你自己刷）
'    4. 起不来就弹窗把日志最后几行给你看 —— start_hidden.vbs 在这种时候是一声不吭的
'
'  用法：把这个文件留在工作台目录里，在桌面上建它的【快捷方式】。
'        （右键 → 发送到 → 桌面快捷方式）
'        不要把文件本身挪到桌面 —— 它靠自己所在的位置找 app.py。
' ============================================================

Dim Q : Q = Chr(34)

Dim fso, ws, baseDir, pyExe, appPy, logFile, port, url
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws  = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyExe   = baseDir & "\venv\Scripts\python.exe"
appPy   = baseDir & "\app.py"
logFile = baseDir & "\workbench.log"

If Not fso.FileExists(pyExe) Then
  MsgBox "找不到虚拟环境：" & vbCrLf & pyExe & vbCrLf & vbCrLf & _
         "请先双击 install.bat 安装。", 16, "工作台"
  WScript.Quit 1
End If
If Not fso.FileExists(appPy) Then
  MsgBox "找不到 app.py：" & vbCrLf & appPy & vbCrLf & vbCrLf & _
         "这个 vbs 要和 app.py 放在同一个目录里。" & vbCrLf & _
         "如果你想放桌面，请改用【快捷方式】。", 16, "工作台"
  WScript.Quit 1
End If

port = ReadPort(baseDir & "\config.json")
url  = "http://127.0.0.1:" & port & "/"

' ---- 1. 关掉占用该端口的旧进程 ----
' 按端口关，不是无差别 kill python.exe —— 别误伤你正在跑的其它脚本
ws.Run "cmd /c for /f " & Q & "tokens=5" & Q & " %a in ('netstat -ano ^| findstr " & _
       Q & ":" & port & Q & " ^| findstr LISTENING') do taskkill /f /pid %a", 0, True
WScript.Sleep 600

' ---- 2. 启动 ----
ws.Run "cmd /c " & Q & Q & pyExe & Q & " " & Q & appPy & Q & _
       " > " & Q & logFile & Q & " 2>&1" & Q, 0, False

' ---- 3. 等它真的起来 ----
Dim i, ok
ok = False
For i = 1 To 40           ' 最多等 20 秒
  WScript.Sleep 500
  If Probe(url & "api/health") Then
    ok = True
    Exit For
  End If
Next

' ---- 4. 开浏览器 / 报错 ----
If ok Then
  ws.Run url, 1, False
Else
  MsgBox "工作台没能启动。" & vbCrLf & vbCrLf & _
         "workbench.log 最后几行：" & vbCrLf & vbCrLf & Tail(logFile, 15), 16, "工作台"
End If


' ============================================================

Function ReadPort(cfgPath)
  ' 从 config.json 里读 server.port，读不到就用 5050
  Dim f, txt, re, m
  ReadPort = 5050
  On Error Resume Next
  If Not fso.FileExists(cfgPath) Then Exit Function
  Set f = fso.OpenTextFile(cfgPath, 1)
  txt = f.ReadAll
  f.Close
  If Err.Number <> 0 Then Exit Function
  Set re = New RegExp
  re.Pattern = """port""\s*:\s*(\d+)"
  re.IgnoreCase = True
  Set m = re.Execute(txt)
  If m.Count > 0 Then ReadPort = CInt(m(0).SubMatches(0))
  On Error GoTo 0
End Function

Function Probe(u)
  Dim h
  Probe = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.XMLHTTP")
  h.Open "GET", u, False
  h.Send
  If Err.Number = 0 Then
    If h.Status = 200 Then Probe = True
  End If
  Err.Clear
  On Error GoTo 0
End Function

Function Tail(path, n)
  Dim f, lines, i, out
  Tail = "(没有日志)"
  On Error Resume Next
  If Not fso.FileExists(path) Then Exit Function
  Set f = fso.OpenTextFile(path, 1)
  lines = Split(f.ReadAll, vbLf)
  f.Close
  If Err.Number <> 0 Then Exit Function
  out = ""
  For i = UBound(lines) - n + 1 To UBound(lines)
    If i >= 0 Then out = out & lines(i) & vbCrLf
  Next
  If Trim(out) <> "" Then Tail = out
  On Error GoTo 0
End Function
