schtasks /create /tn "my155-inspect-30min" /tr "C:\Users\da\my155-inspect\run_inspect.cmd" /sc minute /mo 30 /f
Write-Output "==== QUERY ===="
schtasks /query /tn "my155-inspect-30min" /fo LIST
