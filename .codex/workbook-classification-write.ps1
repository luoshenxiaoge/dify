$ErrorActionPreference = 'Stop'
$source = 'C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理/大模型平台开发工具系统工程功能点清单V0.1(1).xls'
$outDir = Join-Path (Split-Path $PSScriptRoot) 'outputs/01a16409-eacd-7653-9f0a-6547f7ae8607'
$destination = Join-Path $outDir '大模型平台开发工具系统工程功能点清单V0.1(1)-归类复核版.xls'
$plan = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'workbook-classification-plan.json') -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
if (Test-Path -LiteralPath $destination) { throw "Output already exists: $destination" }
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $destination
$app = New-Object -ComObject 'KET.Application'
$book = $null
$alerts = $app.DisplayAlerts
try {
    $app.DisplayAlerts = $false
    $book = $app.Workbooks.Open($destination, 0, $false)
    if ($book.ReadOnly) { throw 'Output opened read-only' }
    foreach ($tab in $plan.edits.Keys) {
        $sheet = $book.Worksheets.Item($tab)
        $updates = $plan.edits[$tab]
        foreach ($address in $updates.Keys) {
            $cell = $sheet.Range($address)
            if ($cell.EntireRow.Hidden) { throw "Hidden target: $tab!$address" }
            $value = $updates[$address]
            if ($value -is [string]) { $cell.Value2 = $value }
            else { $cell.Value2 = [double]$value }
        }
        Write-Output "$tab : $($updates.Count) cells"
    }
    $book.Save()
    Write-Output $destination
} finally {
    if ($null -ne $book) { $book.Close($false) }
    $app.DisplayAlerts = $alerts
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
