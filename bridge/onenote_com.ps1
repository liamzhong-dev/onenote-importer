# -*- coding: utf-8 -*-
# PowerShell helper for driving OneNote desktop COM automation.
# Python 侧通过 subprocess 调用，避免引入 pywin32 依赖（保持零 pip 安装）。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('probe', 'notebooks', 'sections', 'pages', 'ensure-notebook', 'ensure-section', 'create-page', 'get-page', 'update-page', 'goto-page', 'delete-page')]
    [string]$Command,
    [string]$NotebookId = '',
    [string]$SectionId = '',
    [string]$PageId = '',
    [string]$Name = '',
    [string]$XmlFile = '',
    [string]$CreatePath = ''
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ ok = $true; command = $Command }

function Get-NoteApp {
    [pscustomobject]$app = $null
    foreach ($id in @('OneNote.Application.16', 'OneNote.Application.15', 'OneNote.Application')) {
        try { $app = New-Object -ComObject $id; break } catch { }
    }
    return $app
}

function Write-JsonResult($obj) {
    $obj | ConvertTo-Json -Depth 12 -Compress
}

try {
    $app = Get-NoteApp
    if ($null -eq $app) {
        $result.ok = $false
        $result.error = 'no_onenote'
        $result.message = '本机没有可用的桌面版 OneNote（需要 Microsoft Store 之外的桌面版本）'
        Write-JsonResult $result
        exit 0
    }

    switch ($Command) {
        'probe' {
            $xml = ''
            try {
                $null = $app.GetHierarchy('', 2, [ref]$xml)
                $result.version = 'available'
            } catch {
                $result.ok = $false
                $result.error = 'hierarchy_failed'
                $result.message = $_.Exception.Message
            }
        }
        'notebooks' {
            $xml = ''
            $app.GetHierarchy('', 2, [ref]$xml)
            $doc = [xml]$xml
            $nsm = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
            $nsm.AddNamespace('one', 'http://schemas.microsoft.com/office/onenote/2013/onenote')
            $items = @()
            foreach ($n in $doc.SelectNodes('/one:Notebooks/one:Notebook', $nsm)) {
                $items += [ordered]@{ id = $n.GetAttribute('ID'); name = $n.GetAttribute('name') }
            }
            $result.notebooks = $items
        }
        'sections' {
            $xml = ''
            $app.GetHierarchy($NotebookId, 3, [ref]$xml)
            $doc = [xml]$xml
            $nsm = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
            $nsm.AddNamespace('one', 'http://schemas.microsoft.com/office/onenote/2013/onenote')
            # 用 XPath 递归收集，分区组（SectionGroup）里的内容也能拿到
            $nodes = $doc.SelectNodes('//one:Notebook//one:Section', $nsm)
            $items = @()
            foreach ($s in $nodes) {
                $items += [ordered]@{ id = $s.GetAttribute('ID'); name = $s.GetAttribute('name') }
            }
            $result.sections = $items
        }
        'pages' {
            # 列出某个分区下的页面标题，用于「已有同名页面不重复建」
            $xml = ''
            $app.GetHierarchy($SectionId, 4, [ref]$xml)
            $doc = [xml]$xml
            $nsm = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
            $nsm.AddNamespace('one', 'http://schemas.microsoft.com/office/onenote/2013/onenote')
            $items = @()
            foreach ($p in $doc.SelectNodes('//one:Page', $nsm)) {
                $title = ''
                foreach ($t in $p.SelectNodes('.//one:Title//one:T', $nsm)) {
                    $title += $t.InnerText
                }
                if (-not $title) { $title = $p.GetAttribute('name') }
                if (-not $title) { $title = $p.GetAttribute('ID') }
                $items += [ordered]@{
                    id       = $p.GetAttribute('ID')
                    title    = $title.Trim()
                    name     = $p.GetAttribute('name')
                    created  = $p.GetAttribute('dateTime')
                    modified = $p.GetAttribute('lastModifiedTime')
                }
            }
            $result.pages = $items
        }
        'ensure-notebook' {
            $xml = ''
            $app.GetHierarchy('', 2, [ref]$xml)
            $doc = [xml]$xml
            $nsm = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
            $nsm.AddNamespace('one', 'http://schemas.microsoft.com/office/onenote/2013/onenote')
            $found = $null
            foreach ($n in $doc.SelectNodes('/one:Notebooks/one:Notebook', $nsm)) {
                if ($n.GetAttribute('name').Trim() -eq $Name.Trim()) { $found = $n; break }
            }
            if ($null -ne $found) {
                $result.notebook = [ordered]@{ id = $found.GetAttribute('ID'); name = $Name }
            }
            else {
                if (-not $CreatePath) {
                    $CreatePath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) ('OneNote 笔记本\' + $Name)
                }
                try {
                    $newId = ''
                    $app.OpenHierarchy($CreatePath, '', [ref]$newId, 1)
                    $result.notebook = [ordered]@{ id = $newId; name = $Name; created = $true }
                }
                catch {
                    $result.ok = $false
                    $result.error = 'create_notebook_failed'
                    $result.message = $_.Exception.Message + '（COM 通道无法自动建笔记本，请先在 OneNote 里手动新建「' + $Name + '」）'
                }
            }
        }
        'ensure-section' {
            $xml = ''
            $app.GetHierarchy($NotebookId, 3, [ref]$xml)
            $doc = [xml]$xml
            $nsm = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
            $nsm.AddNamespace('one', 'http://schemas.microsoft.com/office/onenote/2013/onenote')
            $found = $null
            foreach ($s in $doc.SelectNodes('//one:Notebook//one:Section', $nsm)) {
                if ($s.GetAttribute('name').Trim() -eq $Name.Trim()) { $found = $s; break }
            }
            if ($null -ne $found) {
                $result.section = [ordered]@{ id = $found.GetAttribute('ID'); name = $Name }
            }
            else {
                # ⚠️ 建分区必须给「文件名」，也就是带 .one 后缀。
                # 传纯显示名会被 OneNote 直接拒掉：OpenHierarchy 报 HRESULT 0x80042004。
                # 建出来的分区显示名会自动去掉 .one。
                $fileName = $Name
                if (-not $fileName.EndsWith('.one')) { $fileName = $fileName + '.one' }
                try {
                    $newId = ''
                    $app.OpenHierarchy($fileName, $NotebookId, [ref]$newId, 3)
                    $result.section = [ordered]@{ id = $newId; name = $Name; created = $true }
                }
                catch {
                    $result.ok = $false
                    $result.error = 'create_section_failed'
                    $result.message = $_.Exception.Message + '（COM 通道建分区失败，请先在 OneNote 里手动新建分区「' + $Name + '」）'
                }
            }
        }
        'create-page' {
            $newId = ''
            $app.CreateNewPage($SectionId, [ref]$newId, 0)
            $result.pageId = $newId
        }
        'get-page' {
            # 取回页面现有 XML，交给 Python 当底稿改。原因见 update-page 注释：
            # 不带 ID 的 Outline 在 UpdatePageContent 里是「追加」，只有拿原页面
            # 当底稿、把里面已有的空 outline 换掉，才不会多出一行空段落。
            if (-not $XmlFile) { throw '缺少 -XmlFile' }
            $xml = ''
            # pageInfo = 3 (piAll)：必须带上正文结构。
            # 用 0 (piBasic) 试过 —— 返回的页面里根本没有 <one:Outline>，
            # 于是「删掉旧 outline 再写新的」变成了「只有新增」，
            # 页面会攒出第二条 outline，旧内容也留在那儿。
            $app.GetPageContent($PageId, [ref]$xml, 3)
            [System.IO.File]::WriteAllText($XmlFile, $xml, (New-Object System.Text.UTF8Encoding($false)))
            $result.bytes = (Get-Item -LiteralPath $XmlFile).Length
        }
        'update-page' {
            if (-not (Test-Path $XmlFile)) {
                throw "XML 文件不存在：$XmlFile"
            }
            $xmlText = [System.IO.File]::ReadAllText($XmlFile, [System.Text.Encoding]::UTF8)
            $app.UpdatePageContent($xmlText)
            $result.ok = $true
        }
        'goto-page' {
            # 写完把 OneNote 拉到这一页，用户一眼就能看到「导进去了」
            if (-not $PageId) {
                $result.ok = $false
                $result.error = 'missing_page_id'
                $result.message = '没有可打开的页面 ID'
            }
            else {
                try {
                    $app.NavigateTo($PageId, '', $false)
                    $result.ok = $true
                }
                catch {
                    $result.ok = $false
                    $result.error = 'goto_failed'
                    $result.message = $_.Exception.Message
                }
            }
        }
        'delete-page' {
            # DeleteHierarchy 的完整签名是 (ID, dateExpectedLastModified, force)。
            # 以前这里把 'moveToRecycleBin' 当第二个参数传 —— 那是字符串，
            # 而这里要的是时间，转换失败就被 catch 吞掉，删除从来没成功过。
            $done = $false
            try {
                $app.DeleteHierarchy($PageId, [DateTime]::MinValue, $true)
                $done = $true
            }
            catch {
                try {
                    $app.DeleteHierarchy($PageId)
                    $done = $true
                }
                catch {
                    $result.ok = $false
                    $result.error = 'delete_failed'
                    $result.message = $_.Exception.Message
                }
            }
            if ($done) { $result.deleted = $true }
        }
    }
}
catch {
    $result.ok = $false
    $result.error = 'exception'
    $result.message = $_.Exception.Message
}

Write-JsonResult $result
