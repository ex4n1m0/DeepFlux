"""Simplified Chinese copy of the in-app User Guide (HELP_HTML).

Kept as a separate module so the English source in gui/help_dialog.py stays
the single editing surface for content changes — when the guide gains a
section or the version bumps, BOTH files must be updated (the "Keep the
User Guide current" rule doubles). Selection happens in HelpDialog at
construction time (restart-to-apply, like the rest of gui/i18n).
"""
from __future__ import annotations

HELP_HTML_ZH = r"""
<!DOCTYPE html>
<html>
<head>
<style>
  body {
    font-family: 'Inter', 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    color: #ffffff;
    background-color: #0a0a0f;
    font-size: 20px;
    line-height: 1.7;
    padding: 8px;
  }
  h1 { color: #2a7abf; font-size: 30px; border-bottom: 3px solid #1a2a4a; padding-bottom: 6px; }
  h2 { color: #2a7abf; font-size: 24px; margin-top: 20px; }
  h3 { color: #80f0ff; font-size: 20px; margin-top: 12px; }
  p { margin: 4px 0; }
  ul { margin: 4px 0; padding-left: 20px; }
  li { margin: 2px 0; }
  code { color: #a8edff; background-color: #0d1117; padding: 1px 4px; border-radius: 3px; font-size: 18px; font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace; }
  table { border-collapse: collapse; width: 100%; margin: 6px 0; }
  th { color: #2a7abf; text-align: left; border-bottom: 3px solid #1a2a4a; padding: 4px 6px; }
  td { border-bottom: 3px solid #111827; padding: 4px 6px; }
  .note { background-color: #0d1117; border-left: 3px solid #2a7abf; padding: 6px 10px; margin: 8px 0; border-radius: 0 4px 4px 0; }
  .warn { background-color: #0d1117; border-left: 3px solid #ffcc00; padding: 6px 10px; margin: 8px 0; border-radius: 0 4px 4px 0; }
</style>
</head>
<body>

<h1>DeepFlux 5.4 —— 用户指南</h1>

<p>DeepFlux 是一个 AI 驱动的下载管理器，内置浏览器、媒体播放器、社区聊天室
和文件管理器。AI 智能体可以搜索并下载种子、查找直链下载、管理你的媒体库 ——
还能操控应用本身：它可以操作浏览器、管理下载队列、播放 IPTV 内容、整理文件，
所有敏感操作都会先征求你的确认。</p>

<h2>快速上手</h2>

<ol>
  <li><b>添加你的 API 密钥：</b>设置 → AI 与 API 密钥：<i>AI 智能体…</i>（选择
      DeepSeek 端点，或经 OpenRouter 使用 DeepSeek），按需调整 Base URL，
      然后粘贴密钥。没有密钥时，智能体以离线演示模式运行。</li>
  <li><b>配置 Jackett（可选但推荐）：</b>下载 → Jackett 设置。Jackett 连接
      种子索引站，为智能体提供结构化的搜索结果。Jackett 没在运行时 DeepFlux
      会自动启动它。如果安装 DeepFlux 结束时你保留了<i>“现在设置 Jackett
      种子搜索”</i>勾选，这一步已经完成 —— 服务已安装、公共索引器已添加、
      API 密钥已关联。要手动重做，请运行
      <code>"C:\Users\&lt;你&gt;\AppData\Local\Programs\DeepFlux\DeepFlux.exe"
      --setup-jackett</code>（会出现一次 Windows 权限提示）。</li>
  <li><b>可选的搜索密钥：</b>添加 Brave 或 Perplexity API 密钥（环境变量
      <code>BRAVE_API_KEY</code> / <code>DEEPSEEK_API_KEY</code> 也可以）。
      之后网页搜索会并行查询 DuckDuckGo、Brave 和 Perplexity 并合并结果 ——
      Perplexity 还会附上一段综合答案。</li>
  <li><b>开始搜索：</b>在智能体标签的输入框里输入你要找的东西并按
      <code>Enter</code>。智能体会搜索、挑选最好的结果，并在下载前询问你。</li>
</ol>

<p><b>窗口大小：</b>窗口可以随意缩小。窗口较窄时，不常用的工具栏按钮会收进
<b>⋯</b> 菜单按钮（播放页、文件管理页的功能键栏和下载列表都是如此），播放器
的控制条也会切换为紧凑按钮 —— 一切仍可触达，任何东西都不会消失。</p>

<h2>页面总览</h2>

<table>
  <tr><th>页面</th><th>用途</th><th>智能体控制</th></tr>
  <tr><td><b>浏览</b></td><td>完整的 Chromium 浏览器（HTML5 全屏视频可用 —— ESC 退出）。
      下载自动进入下载管理器并有通知；<code>.torrent</code> 链接自动添加。</td><td>导航、标签页、读取页面、点击、填表、书签</td></tr>
  <tr><td><b>智能体</b></td><td>带语音输入 (🎤) 的 AI 对话。每次启动智能体都会自动问候。<code>Enter</code> = 智能体搜索；<code>Ctrl+Enter</code> = 快速网页搜索。</td><td>—</td></tr>
  <tr><td><b>下载</b></td><td>上方种子、下方下载管理器。添加任何传输任务时自动聚焦。</td><td>完整队列控制：列表、暂停、恢复、重试、取消</td></tr>
  <tr><td><b>播放</b></td><td>IPTV 与媒体播放器 —— 直播电视、电影、剧集，以及你自己的本地媒体文件夹。</td><td>搜索播放列表、播放、暂停、停止、音量、节目单</td></tr>
  <tr><td><b>文件管理</b></td><td>双栏文件管理器（Double Commander 风格）。</td><td>浏览、复制、移动、重命名、删除、新建文件夹</td></tr>
  <tr><td><b>聊天室</b></td><td>DeepFlux Room —— 内嵌的 OnlyHumans 社区聊天（每个词都是一个房间；默认打开 “deepflux” 房间）。</td><td>—</td></tr>
</table>

<p>用 <code>Ctrl+1</code>–<code>Ctrl+6</code> 或点击左边缘的<b>活动栏</b>切换页面：
<b>智能体</b>、<b>浏览</b>、<b>下载</b>、<b>播放</b>、<b>文件管理</b>和<b>聊天室</b>。
活动栏始终显示你所在的位置，把活动下载数作为角标挂在按钮上，底部还固定着
<b>设置</b>入口 —— <code>Ctrl+,</code> 打开可搜索的<b>设置中心</b>（输入
"jackett" 或 "api key" 就能找到任何设置）。<b>Ctrl+K</b> 打开<b>智能体快速提问
面板</b> —— 在任何页面随时提问；对话与智能体页面共享。底部状态条显示实时
活动标签（下载、智能体状态），🔔 铃铛保留最近事件日志（完成、同步、更新）。
在下载页，视图标签（<b>两者 / 种子 / 下载</b>）让任一列表独占全宽；在播放页，
分区标签直达直播 / 电影 / 剧集 / 收藏 / 最近播放。所有命令都在三个真正的
菜单里：<b>文件</b>（操作）、<b>设置</b>（中心 + 分区）和<b>帮助</b>。</p>

<h2>使用智能体</h2>

<p>像平常说话一样输入 —— 智能体理解自然语言：</p>

<ul>
  <li>“下载一部最近的 1080p 电影” —— 搜索并下载</li>
  <li>“找一个免费视频剪辑软件的直链下载” —— 查找并加入直链 HTTP 下载</li>
  <li>“暂停所有超过 50GB 的任务” —— 批量操作</li>
  <li>“我的下载情况怎么样？” —— 同时显示种子和下载队列</li>
  <li>“重试那个失败的下载” / “取消它并删除未完成的文件”</li>
  <li>“把种子下载速度限制在 2 MB/s” —— 实时会话限速</li>
  <li>“打开一个种子站页面” —— 操控浏览器</li>
  <li>“读取这个页面并点击 24.04 的下载链接” —— 看到的是渲染后的页面（JS、登录态）</li>
  <li>“播放一个新闻频道” / “现在在播什么？” —— IPTV 播放与节目单</li>
  <li>“整理我下载文件夹里的文件” —— 文件操作</li>
  <li>“订阅这个 RSS” —— 管理订阅源</li>
  <li>“帮我找一些 IPTV 播放列表并添加” —— 在网上搜索公共 M3U 播放列表、
      验证后作为播放页源添加</li>
</ul>

<h3>让智能体配置应用</h3>
<p>凡是你在对话框里能配置的东西，智能体都能代劳 —— 它总会先请求确认，
改动与设置对话框一样保存到你的配置里：</p>
<ul>
  <li><b>IPTV 源</b> —— “把这个 M3U 地址添加为源”、“停用第二个播放列表”、
    “移除那个提供商”。播放页立即重新加载。</li>
  <li><b>API 密钥</b> —— “这是我的 TMDb 密钥：…” 会写入。密钥是只写的：
    智能体可以设置或清除，但永远无法读回已有值。</li>
  <li><b>设置</b> —— “把海报缓存设为 5 GB”、“关掉节目单”、“用 VLC 播放”。
    秘密字段同样只能通过密钥工具写入。</li>
  <li><b>搜索源</b> —— 添加/移除种子索引站，立即生效。</li>
  <li><b>缺失组件</b> —— “帮我装 FFmpeg”、“把 Jackett 配好”：智能体可以从
    官方来源下载安装程序并运行（例如通过 <code>winget</code>）。每条命令都会
    先展示给你批准 —— 没有你的确认什么都不会执行。命令以你自己的权限运行，
    系统级安装可能出现正常的 Windows UAC 提示。</li>
</ul>

<p>智能体按这个顺序搜索：Jackett（结构化结果）→ 源站网页搜索 →
RSS 订阅 → 通用网页搜索。快速搜索一无所获时会自动升级为全部 sources 的
完整扫描。网页搜索并行查询所有已配置的服务商（DuckDuckGo 总是参与，
Brave 和 Perplexity 在配置密钥后加入）并合并结果 —— Perplexity 密钥还会
附上一段综合答案。</p>

<h3>语音输入</h3>
<p>智能体输入框旁的 🎤 按钮切换听写：点击开始录音（按钮变红），再点一次停止 ——
转写文字自动发送给智能体（智能体忙时留在输入框里，方便你先检查）。识别完全
本地进行（faster-whisper）—— 无云端、无 API 密钥，音频永不离开你的电脑。
语音模型在首次使用时下载一次到 <code>~/.deeptorrent/models/</code>。可在
<code>~/.deeptorrent/config.json</code> 的 <code>voice</code> 段调整：
<code>model</code>（tiny / base / small / medium / large-v3 —— 越大越准也越慢）、
<code>language</code>（留空 = 自动检测）和 <code>auto_send</code>。</p>

<h3>安全与确认</h3>
<p>有真实副作用的操作会先请求你的确认：开始下载、删除文件、取消下载（会删除
未完成文件）、播放 IPTV 内容、在浏览器里点击或提交表单，以及所有应用配置
改动（添加源、写密钥、改设置）。只读操作（搜索、列表、状态查询）立即执行。</p>

<div class="note">
<b>提示：</b>Jackett 的结果远好于网页搜索。安装它并在
下载 → 源 → 从 Jackett 获取 里添加你的索引器。
</div>

<h2>下载</h2>

<h3>种子</h3>
<ul>
  <li>粘贴磁力链接或打开 <code>.torrent</code> 文件（文件菜单，或拖放）。</li>
  <li>完成的视频种子显示 ▶ —— 双击播放。右键查看更多选项。</li>
  <li><b>边下边播：</b>右键视频种子 → “边下边播”。下载到安全缓冲后即开始
      播放。如果网速低于视频码率，DeepFlux 会测量两者并预先缓冲差值 ——
      播放期间绝不会读过已验证的下载边界（无花屏），并优先下载播放点附近
      的分片。</li>
</ul>

<h3>直接下载</h3>
<ul>
  <li>在下载面板粘贴直链文件 URL，或让智能体去找。</li>
  <li>HLS（<code>.m3u8</code>）和 DASH（<code>.mpd</code>）流会被自动捕获。</li>
  <li>引擎把大文件拆分为分片并行下载，速度更快。</li>
  <li>不报告文件大小的服务器也能正常下载 —— 该行显示动态忙碌条和实时的
      “已下载”计数。</li>
  <li>中断的下载自动续传。</li>
</ul>

<h3>列表列宽</h3>
<p>在种子、下载、分片列表 —— 以及文件管理页的双栏文件面板 —— 列宽自动
适配，文件名始终完整可见，名称列随窗口拉伸。每一列也都可以拖动 —— 把任意
列边缘拖到你喜欢的宽度它会保持不变；双击列边缘恢复自动适配。悬停名称可看
完整文本的工具提示。</p>

<h3>浏览器下载</h3>
<p>内置浏览器自动拦截下载并转入内部分段下载管理器 —— 你会收到通知，下载页
自动聚焦。浏览器的会话 Cookie 会带过去，登录才能下载的文件照常工作。
<code>.torrent</code> 文件是例外：它们走浏览器自己的下载（私有种子站需要完整
会话），完成后加入种子引擎。</p>

<h2>播放页（IPTV 与媒体库）</h2>

<ul>
  <li><b>布局：</b>本页播放器优先打开 —— 视频区占据全部宽度。<b>🗂 分类树</b>和
      <b>🖼 内容</b>工具栏按钮显示/隐藏分类树和海报/频道面板；每个面板重新
      打开时保持你上次拖到的宽度。</li>
  <li><b>源：</b>播放 → 播放列表源 —— M3U 地址或文件、Xtream Codes 登录，
      或本地媒体文件夹（你自己的电影/剧集库，像任何提供商一样被扫描并匹配
      海报）。所有启用的源同时加载；侧栏树为 源 → 分区 → 分类。安装时附带
      一个小型示例播放列表（澳门 —— iptv-org 的 7 个免费频道），首次启动
      就有内容可看；不想要就在那里移除。</li>
  <li><b>按年份分组：</b>电影和剧集在各分类下按上映年份子分组（2026、
      2025、……），最新在前；没有年份的条目归入“其他”。树上方的“分组”
      选择器可切回平铺分类。</li>
  <li><b>浏览：</b>单击打开信息面板；双击播放。网格和列表两种视图，搜索框
      跨所有内容过滤。</li>
  <li><b>文件夹搜索：</b>打开搜索框旁的 <b>📍 文件夹</b>，查询就只在树中
      你最后点击的文件夹内进行（分类、年份、分区或整个源 —— 含子文件夹）。
      占位符显示当前文件夹；限定范围时点击其他文件夹会用同一查询重新过滤。
      关闭即恢复全播放列表搜索。</li>
  <li><b>下载点播内容：</b>IPTV 源里的电影和剧集可以保存到磁盘。右键电影或
      剧集（网格或列表视图），或用信息面板的 <b>⬇ 下载</b> 按钮；在剧集的
      分集列表里，右键单集可下载该集或整季。整部剧/整季会先询问。下载在
      下载管理器中进行（与浏览器和智能体下载同一处）：单个文件进入默认
      文件夹，分集进入以剧名命名的文件夹。HLS/DASH 流会被捕获并重封装为
      MP4；源级的 User-Agent/Referer 设置会被应用。</li>
  <li><b>海报：</b>你浏览时海报和频道台标自动补齐，其余由一个刻意放慢的
      后台扫描查找（状态栏的“正在查找海报 n/N”计数），以遵守 API 限额。
      海报墙随窗口自适应 —— 每行 3 到 9 张：拉宽窗口看到更多，收窄看到
      更大。</li>
  <li><b>节目单：</b>播放列表声明了指南地址时，直播频道显示正在播/接下来；
      也可以在每个源的设置里单独指定。</li>
  <li><b>刷新韧性：</b>提供商刷新失败时，屏幕保留最后一次成功的频道列表，
      并标记为缓存数据。</li>
</ul>

<h2>媒体播放器</h2>

<h3>多画面 —— 4×4 网格</h3>
<p>播放器工具行的 <b>▦ 4×4</b> 按钮把视频区替换为 4×4 的独立画面格。每格
播放自己的片段、有自己的声音 —— 每格初始<b>静音</b>，填满网格也不会炸响；
用每格控制条的 🔇 打开你想要的声音。</p>
<ul>
  <li><b>载入一格：</b>右键空格 → <i>分配本地文件…</i> 或 <i>分配 IPTV
      频道…</i>（搜索你的播放列表）。同一时刻只有<b>一格</b>能播直播流 ——
      提供商拒绝多个并发连接；其余 15 格用本地文件。</li>
  <li><b>批量填充：</b>右键空格 → <i>用文件填满空格…</i> —— 多选文件后按
      字母序发到空格（编号剧集正好排齐）。</li>
  <li><b>文件夹自动轮播：</b>右键任意格 → <i>播放文件夹（自动轮播）…</i>
      —— 文件夹前 16 个视频填满网格，某格片段结束就自动播放文件夹里下一个
      未播的视频。每个视频只播一次；文件夹播完后，各格随其最后一个片段
      结束自行清空。</li>
  <li><b>逐格控制</b>（每格下方的控制条）：⏸/▶ 播放/暂停、🔇 静音、音量
      滑块、⛶ 把该片段移到主播放器（完整控制：轨道、字幕、录制），以及
      ✕ 清空此格。手动清空的格子保持空白；轮播只对真正结束的片段作出
      反应。</li>
  <li><b>全部跳过：</b>网格下方的横条有 ⏪ −60s / ⏪ −10s / ⏩ +10s /
      ⏩ +60s 按钮 —— 所有已载入片段一起跳转，连按继续（每个片段各自
      限制在起止范围内）。</li>
  <li>画面格不做后期处理、缓冲极小，十六路同时播放互不拖累。在主播放器
      开始播放（或提升某格）会退出网格模式并停止所有画面格。</li>
</ul>

<ul>
  <li><b>杜比视界与 HDR：</b>杜比视界（profile 5/7/8）和 HDR10 开箱即用，
      色彩与动态色调映射正确（mpv 后端）。VLC 后端可在 播放 → 播放设置 里
      选用以兼容。</li>
  <li><b>音轨：</b>点击控制条的 🎧（或按 <code>#</code> 循环切换）。</li>
  <li><b>字幕：</b>点击控制条的 CC（或按 <code>J</code> 循环切换；列表里有
      “关闭”）。</li>
  <li><b>下载字幕：</b>CC 菜单 → “在线查找字幕…” 搜索 OpenSubtitles.com 并
      把所选文件加载到正在播放的视频。本地文件按精确文件哈希匹配；流媒体按
      标题搜索。保存在视频旁边（流媒体存到 <code>~/.deeptorrent/subtitles/</code>），
      下次自动加载。需要免费 API 密钥 —— 播放 → 字幕与语言；添加账号凭据
      可提高每日配额。智能体也行：“加载英文字幕” 自动挑选最佳匹配（精确
      哈希 &gt; 你的首选语言 &gt; 下载量最多）。</li>
  <li><b>首选语言：</b>播放 → 字幕与语言。多轨文件在播放开始时自动切换到
      你的语言的音轨/字幕。</li>
  <li><b>音乐可视化：</b>音频文件播放时以 MilkDrop（Butterchurn）可视化代替
      黑屏 —— 螺旋按钮选择预设，你也可以把自己的 <code>.milk</code> 文件放进
      <code>~/.deeptorrent\presets</code>。</li>
  <li><b>平滑运动：</b>视频锁定到你屏幕的刷新率，播放 → 播放设置 可以混合
      帧以消除抖动（仅文件和点播 —— 直播用所需的普通同步以避免重启）。它
      平滑的是<i>不匹配的</i>帧率；它不凭空造帧，所以 120 Hz 屏幕上的 24 fps
      电影本来就完美对齐、看起来不变。要真正的高帧率运动，见下面的 SVP。</li>
  <li><b>SVP 动态补帧（肥皂剧效果）：</b>通过生成中间帧把 24 fps 电影变成
      真正的高帧率视频（实测：23.976 → 119.88 fps）。这需要
      <a href="https://www.svp-team.com/">SVP 4</a> —— 一个单独的付费程序
      （约 $25 一次性，30 天免费试用）—— 不捆绑任何东西，没有它 DeepFlux
      一切照旧。装了 SVP 4（<b>含其 mpv 播放组件</b>）后自动启用 —— 在
      播放 → 播放设置 取消“SVP 动态补帧”可关闭。适用于本地文件和点播，
      消耗 GPU；直播始终不用 SVP。找不到 SVP 时选项保持灰色，SVP 出故障时
      DeepFlux 自动回退到普通播放。</li>
  <li><code>A</code> 循环画面比例；<code>F</code> 或双击全屏。<code>Shift+F</code>
  （或 <b>🖥 纯净</b> 按钮）进入<b>纯净全屏</b> —— 更彻底的全屏：所有控制始终
  隐藏，只看视频。键盘快捷键（空格、方向键、<code>M</code>……）仍然可用；按
  <code>Esc</code> 或 <code>Shift+F</code> 恢复自动隐藏的控制栏，再按一次
  <code>Esc</code> 退出全屏。</li>
</ul>

<h2>站点抓取</h2>
<p>浏览器工具栏的放大镜按钮按关键词搜索<b>任意视频站点</b>。<b>站点</b>框会
预填浏览器当前打开的站点（粘贴站点上任意地址即可切换）；输入关键词后点击
搜索。抓取器自己找到站点的搜索页（其搜索表单或常见 URL 模式 —— 生效的
模板会显示出来，你也可以用 <code>{query}</code> 自定义），列出结果（缩略图、
时长、标题），点击下载时把每个视频页解析到其内嵌的流 —— 包括藏在混淆
播放器脚本里的流 —— 并作为普通 HLS/DASH/文件任务排入下载管理器。失败的
项保持勾选，方便只重试它们。</p>
<p>想零点击？打开<b>自动排队</b>：每次搜索（以及你浏览的每个页面）都会自动
解析并排队，直到<b>上限</b> —— 关键词进，下载出。该设置会被记住。</p>

<h2>Chrome 扩展</h2>
<p>Chrome 扩展把外部浏览器里的下载发送给 DeepFlux。从安装目录的
<code>_internal\chrome_extension\</code> 文件夹安装（Chrome →
<code>chrome://extensions</code> → 开发者模式 → 加载已解压的扩展程序）。</p>

<h2>设置</h2>

<p>一切都在<b>文件</b>菜单里，扁平排布 —— 按分区分组，每个分区由一条分隔线
和一个加粗标题（API 密钥、浏览器设置、下载、播放）开启，条目略微缩进；
不用层层挖子菜单。唯一的例外是书签文件夹树 —— 它在浏览器工具栏的书签
按钮上。</p>

<table>
  <tr><th>文件菜单分区</th><th>可配置内容</th></tr>
  <tr><td><b>API 密钥</b></td><td>每个服务一页（AI 智能体、种子与网页搜索、
      影视元数据、成人元数据、字幕）：LLM 端点、Base URL 与密钥、Jackett、
      Brave、Perplexity、TMDb、OpenSubtitles。此外还有文件关联，以及
      <b>备份/恢复设置</b> —— 一个口令加密的 <code>.dfc</code> 全量备份
      （密钥、源、全部设置），可妥善保存或搬到另一台电脑。导入在重启后
      生效；原设置保留为 <code>config.json.bak</code>。</td></tr>
  <tr><td><b>浏览器设置</b></td><td>主页、隐私/数据、历史、页面存为 PDF、开发者工具。</td></tr>
  <tr><td><b>书签按钮</b>（浏览器工具栏，无痕按钮左侧）</td><td><b>导入书签…</b> / <b>导出书签…</b> 以及整个书签文件夹树，全在一个弹出窗口 —— 书签在这里，不在文件菜单。</td></tr>
  <tr><td><b>下载</b></td><td>添加磁力 / 添加种子文件、<b>Jackett 设置</b>（URL；测试连接会同步索引器列表）、下载设置各页（种子下载、种子队列、下载管理器 —— 保存路径、带宽限制、连接数）、<b>源</b>（智能体搜索哪些站）、以及 <b>RSS 订阅</b>（订阅源 —— 或者直接让智能体办）。</td></tr>
  <tr><td><b>播放</b></td><td>四个小页面：播放列表源、元数据与缓存（海报缓存、EPG）、字幕与语言（首选音频/字幕语言）、播放设置（后端、解码、缓冲、平滑运动、SVP 补帧、限速）。</td></tr>
</table>

<h2>DeepFlux Room —— 社区聊天</h2>

<p><b>聊天室</b>页面就是 <span style="color:#a8edff">DeepFlux Room</span>，
本应用的社区聊天。它内嵌 <b>OnlyHumans</b> 门户 —— 与
onlyhumans.deepflux.space/join 上同一个房间应用 —— 所以 DeepFlux 里的房间
永远是聊天程序的最新版本。打开时已预填共享社区房间词 <b>“deepflux”</b>，
名字留空：随便取个名按<b>进入房间</b>。在 OnlyHumans Windows 应用或浏览器
门户里输入同一个词的人，和你处在同一个房间。</p>

<ul>
  <li><b>记住我：</b>在加入界面勾选<i>“在这台设备上记住我的名字和房间”</i>，
  DeepFlux 会把你的名字和最近房间词存在自己的存储里 —— 下次一键即可加入。
  不勾选则每次全新开始。</li>
  <li><b>房间：</b>在加入界面输入任何词就进入另一个房间 —— 同词 = 同房间，
  在哪里都一样（DeepFlux、OnlyHumans 应用、任何浏览器）。可猜到的词<i>不是</i>
  访问控制 —— 它只是便利的隔离；每个词都经内存困难函数拉伸，房间词无法
  被暴力破解。</li>
  <li><b>一切都在页面里：</b>消息、图片和小文件、资料、两人私聊，以及房主的
  <b>✦ Warp</b>（轮换房间密钥：在场者一起换到新密钥，之后用同词加入的人会
  落在一个单独的空房间）都在内嵌的门户页里 —— 网站更新后新聊天功能自动
  出现在这里。<b>↻ 刷新</b> 刷新页面；<b>在浏览器中打开</b> 在你的网页浏览器
  里打开同一房间。</li>
  <li><b>加密与投递：</b>每条消息都在你的设备上封缄（端到端；逐消息密钥、
  填充帧），经由 OnlyHumans hub 的密封邮箱投递（“⇄ 站点”路径 —— hub 只存
  它无法读取的签名和密文）。与门户用户的聊天近乎即时；OnlyHumans <i>桌面
  应用</i>上的用户可能要等最多半分钟才能看到并回复（他们的应用按较慢的
  周期收信）。你的聊天身份是本机生成的密钥对，保存在 DeepFlux 自己的
  存储里 —— 永不离开。</li>
</ul>

<h2>隐私</h2>

<p>只要你愿意，DeepFlux 可以完全离线工作。默认只有三样东西连接互联网：</p>

<ul>
  <li><b>匿名使用统计</b> —— 应用运行期间，每隔几分钟向 deepflux.space 发送
  一个极小的 ping，供网站显示实时的“在线人数”。它只带一个随机安装 id
  （磁盘上的一个文件，与你本人无关）、应用版本和操作系统名 —— 仅此而已：
  没有用户名、没有机器名、没有文件名、没有任何浏览或下载活动。可在
  设置 → 下载 → 下载管理器设置 里关闭。</li>
  <li><b>更新检查</b> —— 每日检查 YouTube 下载器是否过期（仅提醒）。同一
  设置页。</li>
  <li><b>聊天室</b> —— 加入期间，房间与 OnlyHumans hub
  （onlyhumans.deepflux.space）保持连接：签名的在场记录和密封信封。hub
  知道<i>有</i>一个应用在线、有哪些密封信封在流动，但永远看不到消息、房间词
  或你的名字。</li>
</ul>

<p>其余一切 —— 搜索、下载、播放、智能体 —— 只在你要求时才使用网络。</p>

<h2>键盘快捷键</h2>

<table>
  <tr><th>按键</th><th>作用</th></tr>
  <tr><td><code>Ctrl+1</code>–<code>Ctrl+6</code></td><td>切换页面（智能体 / 浏览 / 下载 / 播放 / 文件管理 / 聊天室）</td></tr>
  <tr><td><code>Ctrl+K</code></td><td>智能体快速提问面板（任意页面）</td></tr>
  <tr><td><code>Ctrl+,</code></td><td>打开设置菜单</td></tr>
  <tr><td><code>Ctrl+F</code></td><td>跳到智能体输入框</td></tr>
  <tr><td><code>Ctrl+M</code> / <code>Ctrl+O</code></td><td>添加磁力 / 打开种子文件</td></tr>
  <tr><td><code>Ctrl+T</code> / <code>Ctrl+W</code></td><td>新建 / 关闭浏览器标签页</td></tr>
  <tr><td><code>Ctrl+L</code></td><td>聚焦地址栏</td></tr>
  <tr><td><code>Enter</code> / <code>Ctrl+Enter</code></td><td>智能体搜索 / 快速网页搜索</td></tr>
  <tr><td><code>空格</code> / <code>M</code> / <code>F</code></td><td>播放器：暂停 / 静音 / 全屏</td></tr>
  <tr><td><code>Shift+F</code></td><td>播放器：纯净全屏（隐藏所有控制，只看视频）</td></tr>
  <tr><td><code>A</code> / <code>J</code> / <code>#</code></td><td>播放器：画面比例 / 字幕 / 音轨</td></tr>
  <tr><td><code>F5</code> <code>F6</code> <code>F7</code> <code>F8</code> <code>F2</code></td><td>文件管理：复制、移动、新建文件夹、删除、重命名</td></tr>
  <tr><td><code>Ctrl+Q</code></td><td>退出</td></tr>
</table>

<h2>保持 DeepFlux 最新</h2>

<p>DeepFlux 自动检查新版本（每天一次，仅 Windows 安装版 —— 勾选框在
下载 → 下载设置）。有新版本时，一个小对话框提供<b>立即更新</b>、<b>稍后提醒</b>
和<b>跳过此版本</b>。也可以随时通过 <b>帮助 → 检查更新…</b> 手动检查。</p>

<p><b>立即更新</b> 会下载新的安装文件并校验，然后关闭 DeepFlux、安装新版本
并重启 —— 无需去网站下载、无需安装向导。没有你的点击，任何东西都不会被
安装。你的源、API 密钥和设置全部保留（更新在原地重装并保留配置），活动
下载自动暂停并在之后继续。更新日志在 <code>~/.deeptorrent/logs/update.log</code>，
需要时可以查看。自动检查可在 下载 → 下载设置 里关闭。</p>

<h2>疑难解答</h2>

<div class="warn">
<b>安装时出现“Windows 已保护你的电脑”：</b>安装文件未签名，SmartScreen
会在首次下载时警告。文件是安全的 —— 点击<b>更多信息 → 仍要运行</b>。要彻底
跳过警告，可在运行前右键下载的安装文件 → <b>属性</b> → 勾选<b>解除锁定</b> →
确定。足够多的人安装该版本后，警告会自行消失。
</div>

<div class="warn">
<b>智能体没有响应：</b>检查 设置 → AI 与 API 密钥 里的 API 密钥。内置共享
AI 密钥随每个 DeepFlux 新版本轮换，过旧的副本可能失去 AI 访问 —— 更新到
最新版本（帮助 → 检查更新…），或设置你自己的密钥以完全独立于共享密钥。
（macOS 和 Linux 版不带内置密钥 —— 请在 设置 → AI 与 API 密钥 下添加你
自己的。）
</div>

<div class="warn">
<b>搜索不到结果：</b>确认 Jackett 正在运行且你的源已启用（下载 → 源）。
没有 Jackett 时智能体退回较慢的网页搜索。要一次性配好，运行
<code>--setup-jackett</code> 启动 DeepFlux（见快速上手），或重新运行
DeepFlux 安装程序并保留<i>“现在设置 Jackett 种子搜索”</i>勾选。
</div>

<div class="warn">
<b>HLS 下载失败：</b>该流可能受 DRM 保护。DeepFlux 只支持非 DRM 或
AES-128 加密的流。
</div>

<div class="warn">
<b>Chrome 扩展连不上：</b>确认 DeepFlux 正在运行。若原生宿主未注册，请重新
安装。
</div>

<h2>文件位置</h2>

<table>
  <tr><td><code>~/.deeptorrent/config.json</code></td><td>全部设置</td></tr>
  <tr><td><code>~/.deeptorrent/memory/</code></td><td>智能体的持久笔记</td></tr>
  <tr><td><code>~/.deeptorrent/models/</code></td><td>语音识别模型（whisper）</td></tr>
  <tr><td><code>~/Downloads/DeepFlux/</code></td><td>默认下载文件夹</td></tr>
</table>

<h2>版本</h2>
<p>DeepFlux 5.4 —— AI 深度搜索</p>
<p>AI：DeepSeek / OpenRouter / 自定义 · 网页搜索：DuckDuckGo、Brave、Perplexity（并行）</p>

</body>
</html>
"""
