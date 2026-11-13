$ErrorActionPreference='Stop'
$bin='C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies'
$env:PYTHONPATH='D:/PycharmProjects/dify-1104/.codex/workbook-classification-libs'
$src='C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理/大模型平台开发工具系统工程功能点清单-V0.2.xls'
$out='C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理/大模型平台开发工具系统工程功能点清单-V0.2-RS统一副本.xls'
& "$bin/python/python.exe" -X utf8 .codex/workbook-v02-rs.py $src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$plan=Get-Content -LiteralPath .codex/workbook-v02-rs-plan.json -Raw -Encoding UTF8 | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $src).Hash -ine $plan.source_hash){throw 'Original workbook has changed'}
if (Test-Path -LiteralPath $out){throw 'Destination already exists'}
$marker='C:/Users/Administrator/.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.1020.11127/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs'
& "$bin/node/bin/node.exe" $marker --operation-kind edit --expected-output-count 1 --output-format xls
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Copy-Item -LiteralPath $src -Destination $out
$app=New-Object -ComObject 'KET.Application'
$alerts=$app.DisplayAlerts; $book=$null
try {
    $app.DisplayAlerts=$false
    $book=$app.Workbooks.Open($out,0,$false)
    if($book.ReadOnly){throw 'Copy opened read-only'}
    $sheet=$book.Worksheets.Item('附件4-2')
    foreach($u in $plan.rows){
        $r=$sheet.Range("R$($u.row)")
        if($r.EntireRow.Hidden){throw "Unexpected hidden row $($u.row)"}
        if($u.change_r){$r.Value2=[double]$u.date}
        if($u.change_s){$sheet.Range("S$($u.row)").Value2=[string]$u.version}
        $r.NumberFormat='yyyy"年"m"月"d"日"'
        $actual=[string]$r.Text
        $expected=([datetime]::FromOADate([double]$u.date)).ToString("yyyy'年'M'月'd'日'")
        if($actual -cne $expected){throw "Unexpected date display at row $($u.row): $actual / $expected"}
    }
    $book.Save()
}finally{
    if($book){$book.Close($false)}
    $app.DisplayAlerts=$alerts
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
& "$bin/python/python.exe" -X utf8 .codex/workbook-v02-rs.py $src $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Get-Item -LiteralPath $out | Format-List Name,Length
