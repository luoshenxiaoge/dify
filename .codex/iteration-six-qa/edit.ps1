$ErrorActionPreference='Stop'
$source='C:/Users/Administrator/Desktop/工作文档/邮储/需求文档/迭代六/迭代六-智能体平台(1).doc'
$expectedHash='2C01407449E163DCC9C87893CEC6E2B75F755783655F1124488FBDB4C11D8375'
if ((Get-FileHash -LiteralPath $source).Hash -ne $expectedHash) { throw 'Source has changed since inspection' }
$spec=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'requirements.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$stage=Join-Path $PSScriptRoot 'edited.doc'
$pdf=Join-Path $PSScriptRoot 'edited.pdf'
Copy-Item -LiteralPath $source -Destination $stage -Force
$app=New-Object -ComObject 'KWPS.Application'
$alerts=$app.DisplayAlerts; $app.DisplayAlerts=0
$doc=$null

function Get-ParaRecord($p) {
    $r=$p.Range
    $num=$r.ListFormat.ListString
    [pscustomobject]@{ Text=$r.Text; Num=$num; Style=$p.Style.NameLocal; Outline=$r.ParagraphFormat.OutlineLevel }
}

function Set-ParaText([int]$i,[string]$value) {
    $r=$doc.Paragraphs.Item($i).Range
    if(-not $r.Text.EndsWith([string][char]13)){throw "Paragraph $i has no terminal paragraph mark"}
    $r.End=$r.End-1
    $r.Text=$value
}

try {
    $doc=$app.Documents.Open($stage,$false,$false,$false)
    if($doc.ReadOnly){throw 'Staged file is read-only'}
    if($doc.Paragraphs.Count -ne 416){throw 'Unexpected source paragraph count'}
    $start=348; $after=376; $width=$after-$start
    $p1=$doc.Paragraphs.Item($start).Range
    $p2=$doc.Paragraphs.Item($after).Range
    if($p1.ListFormat.ListString -ne '2.1' -or $p2.ListFormat.ListString -ne '3'){throw 'Unexpected source headings'}
    $prefix=@(for($j=1;$j -lt $start;$j++){Get-ParaRecord $doc.Paragraphs.Item($j)})
    $suffix=@(for($j=$after;$j -le $doc.Paragraphs.Count;$j++){Get-ParaRecord $doc.Paragraphs.Item($j)})
    $tail=@(for($j=366;$j -lt $after;$j++){Get-ParaRecord $doc.Paragraphs.Item($j)})
    $template=$doc.Range($p1.Start,$p2.Start)
    for($n=0;$n -lt 2;$n++) {
        $where=$doc.Paragraphs.Item($after+$n*$width).Range.Start
        $insert=$doc.Range($where,$where)
        $insert.FormattedText=$template.FormattedText
    }
    if($doc.Paragraphs.Count -ne 416+2*$width){throw "Template copy produced $($doc.Paragraphs.Count) paragraphs"}
    $deleted=0
    for($n=2;$n -ge 0;$n--) {
        $base=$start+$n*$width
        $item=$spec[$n]
        Set-ParaText $base $item.title
        Set-ParaText ($base+2) $item.overview
        $doc.Paragraphs.Item($base+9).Range.ParagraphFormat.KeepWithNext=-1
        $doc.Paragraphs.Item($base+10).Range.ParagraphFormat.KeepWithNext=-1
        for($i=0;$i -lt $item.flow.Count;$i++) {
            Set-ParaText ($base+4+$i) ('{0}. {1}' -f ($i+1),$item.flow[$i])
        }
        for($i=0;$i -lt $item.rules.Count;$i++) {
            Set-ParaText ($base+11+$i) ('{0}. {1}' -f ($i+1),$item.rules[$i])
        }
        for($j=17;$j -ge 11+$item.rules.Count;$j--){[void]$doc.Paragraphs.Item($base+$j).Range.Delete(); $deleted++}
        for($j=8;$j -ge 4+$item.flow.Count;$j--){[void]$doc.Paragraphs.Item($base+$j).Range.Delete(); $deleted++}
    }
    $newAfter=$after+2*$width-$deleted
    if($doc.Paragraphs.Count -ne $prefix.Count+3*$width-$deleted+$suffix.Count){throw 'Unexpected final paragraph count'}
    foreach($range in @(@{i=1; records=$prefix},@{i=$newAfter; records=$suffix})) {
        for($j=0;$j -lt $range.records.Count;$j++) {
            $old=$range.records[$j]
            $new=Get-ParaRecord $doc.Paragraphs.Item($range.i+$j)
            if($old.Text -cne $new.Text -or $old.Num -cne $new.Num -or $old.Style -cne $new.Style -or $old.Outline -ne $new.Outline){throw "Outside scope changed at paragraph $($range.i+$j): $($old.Text) -> $($new.Text)"}
        }
    }
    $at=$start
    for($n=0;$n -lt 3;$n++) {
        $item=$spec[$n]; $key="2.$($n+1)"
        foreach($k in 0..3) {
            $index=@($at,($at+1),($at+3),($at+4+$item.flow.Count))[$k]
            $expected=if($k -eq 0){$key}else{"$key.$k"}
            $h=Get-ParaRecord $doc.Paragraphs.Item($index)
            if($h.Num -ne $expected){throw "Heading number $($h.Num) should be $expected at $index"}
            Write-Output "$expected $($h.Text.Trim())"
        }
        $at += 6+$item.flow.Count+$item.rules.Count
        for($j=0;$j -lt $tail.Count;$j++) {
            $new=Get-ParaRecord $doc.Paragraphs.Item($at+$j)
            $old=$tail[$j]
            if($new.Text -cne $old.Text -or $new.Style -cne $old.Style){throw "Template .4-.8 changed at $($at+$j)"}
            if($j % 2 -eq 0 -and $new.Num -ne "$key.$([int]($j/2)+4)"){throw "Template .4-.8 numbering unexpected: $($new.Num)"}
        }
        $at+=$tail.Count
    }
    if($at -ne $newAfter -or $doc.Tables.Count -ne 3 -or $doc.Sections.Count -ne 2){throw 'Unexpected section/table structure'}
    $doc.Save()
    $doc.ExportAsFixedFormat($pdf,17)
    Write-Output "Rendered $($doc.ComputeStatistics(2)) pages"
    $doc.SaveAs2((Join-Path $PSScriptRoot 'edited-qa.docx'),16)
} finally {
    if($null -ne $doc){$doc.Close(0)}
    $app.DisplayAlerts=$alerts
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
