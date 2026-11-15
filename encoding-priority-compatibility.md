# w3lib 网页编码来源优先级与兼容性分析报告

- 分析对象：`w3lib 2.6.0`（仓库当前 `task/14`，提交 `824ca5e`），编码实现位于 `w3lib/encoding.py`
- 实际引擎：`frostwork 0.2.2`（Rust 原生扩展 `frostwork._frostwork` + Python 封装 `frostwork/encoding.py`），w3lib 的编码检测、BOM、meta 预扫描、UTF-8 判定、TLD 猜测与 WHATWG 解码全部委托给它
- 验证环境：Python 3.14.2（`/private/tmp/npvenv/bin/python -I`），`tests/test_encoding.py` 97 项全部通过
- 复现方式：所有字节样例均在独立目录 `/tmp/w3lib-encoding-probe/` 的 `probe*.py` 中，以 `python -I` 运行（脚本内显式 `sys.path.insert` 指向仓库，不依赖当前目录），报告中的每条输出均由这些脚本实测得到

---

## 1. 编码决策总链路（新流程，未使用弃用参数时）

`html_to_unicode(ct_header, body, *, url=None)` 与 `html_encoding(...)` 的判定顺序（`w3lib/encoding.py:275-282`、`frostwork/encoding.py:84-94`、原生 `detect_encoding` 存根说明）：

```
0. BOM-less UTF-16 前缀（UTF-16LE/BE 的 <?xml 声明，长度 0 的“BOM”）
1. BOM（EF BB BF / FE FF / FF FE；UTF-32LE BOM 按 UTF-16LE 读，UTF-32BE BOM 不识别）
2. HTTP Content-Type 头中的 charset（经 w3lib 正则提取 + WHATWG 标签解析；无法解析的标签视为“没有头”）
3. <meta charset> / <meta http-equiv=content-type> / 文档起始处 <?xml encoding?> 预扫描
4. 若整个 body 是合法 UTF-8 → UTF-8
5. 否则按字节特征 + url 顶级域猜测（Firefox 旧版 universal charset detector 模型）
```

关键性质：**第 2、3 步中“无法解析为 Encoding Standard 编码的标签会被静默忽略并继续向下走”**，而一旦某一步给出可解析的编码，后续步骤一律不再参与。`html_encoding()` 与 `html_to_unicode()` 走完全相同的新流程，前者只返回编码不解码，二者结果必然一致（测试 `_assert_encoding` 有断言）。

---

## 2. BOM：最高优先级，且会被剥离

### 2.1 实测字节样例

| 输入字节（前缀） | `bom_encoding` → (WHATWG 名, 长度) | `read_bom` → (Python codec, BOM 字节) |
|---|---|---|
| `EF BB BF 68 65 6c 6c 6f` | `('UTF-8', 3)` | `('utf-8', b'\xef\xbb\xbf')` |
| `FE FF 6c 34`（水，UTF-16BE） | `('UTF-16BE', 2)` | `('utf-16-be', b'\xfe\xff')` |
| `FF FE 34 6c`（水，UTF-16LE） | `('UTF-16LE', 2)` | `('utf-16-le', b'\xff\xfe')` |
| `00 00 FE FF …`（UTF-32BE BOM） | `None` | `(None, None)` —— UTF-32 不是 Web 编码 |
| `FF FE 00 00 …`（UTF-32LE BOM） | `('UTF-16LE', 2)` | `('utf-16-le', b'\xff\xfe')` —— 按 UTF-16LE 读 |
| `<?xml …?>` 的 UTF-16LE 字节，无 BOM | `('UTF-16LE', 0)` | `('utf-16-le', b'')` —— 空 BOM |
| 空 body / 普通 ASCII | `None` | `(None, None)` |

### 2.2 优先级与对页面文本的影响

- BOM 压倒一切：即使头声明 `iso-8859-1`、meta 声明 `big5`、url 是 `.jp`，`EF BB BF` 开头的 body 仍按 UTF-8 解码：

  ```python
  html_to_unicode("text/html; charset=iso-8859-1",
                  b"\xef\xbb\xbf" + b'<meta charset="big5">\xc2\xa3',
                  url="http://example.jp/")
  # -> ('utf-8', '<meta charset="big5">£')   # BOM 三字节被剥离，不出现在文本里
  ```

- 剥离规则：`html_to_unicode` 解码的是 `body[length:]`（`encoding.py:278`），UTF-8/16 的 BOM 不会变成 `U+FEFF` 残留。UTF-32LE 被误读为 UTF-16LE 时，body 每两个 ASCII 字符之间会出现 `\x00`（实测 `('\xff\xfe\x00\x00h\x00i\x00') → ('utf-16-le', '\x00hi')`）。
- 无 BOM 的 `utf-16` 标签默认小端（Encoding Standard 规定），实测 `"hi".encode("utf-16-le")` → `('utf-16-le', 'hi')`。
- BOM-less UTF-16 识别只认文档最开头的 XML 声明：`b'<\x00?\x00x\x00m\x00l\x00…'` 与 `b'\x00<\x00?…'` 分别判为 UTF-16LE/BE，且该判定位于标签参数之前——`detect_encoding(body, 'utf-8')` 仍返回 `UTF-16LE/BE`。普通 `<html>` 的 UTF-16 字节无此前缀识别。
- 错误处理影响：BOM 之后的非法字节由对应 WHATWG 解码器替换为 `U+FFFD`，函数不抛异常，例如 `EF BB BF 57 4f 52 44 e3 ab 57…` → `'WORD�WORD2'`。

---

## 3. HTTP 响应头 charset：引号、重复参数、逗号合并头

提取逻辑在 w3lib 自身（`encoding.py:27-78`），分两条路径：

- 快速正则 `_HEADER_ENCODING_RE`：匹配第一个 `(^|;)[ \t]*charset=`，值要么是双引号包裹的 `[\w-]+`，要么是裸 token；值后必须紧跟空白、`;`、`,` 或结尾。
- 若匹配位置之前出现过 `"`，改走 `_quoted_aware_charset` + `_HEADER_PARAMETER_RE`，按 MIME 参数语法整体消费 quoted-string（引号内内容不透明，其中的 `charset=` 不是参数）。
- 提取到的标签统一过 `resolve_encoding()`（WHATWG 标签表），无法解析则返回 `None`。

### 3.1 可复现实测表

| Content-Type 头 | 返回 | 说明 |
|---|---|---|
| `text/html; charset=ISO-8859-4` | `'iso8859-4'` | 大小写不敏感，canonical 名 |
| `text/html; charset="utf-8"` | `'utf-8'` | 双引号合法 |
| `text/html; charset = utf-8` | `None` | `=` 两侧不允许空白 |
| `text/html; charset= "utf-8"` | `None` | 引号前不允许空白 |
| `text/html; charset='utf-8'` | `None` | 单引号不是 HTTP quoted-string，被当作 token 字符而失败 |
| `text/html; charset="utf-8` / `charset=utf-8"` | `None` | 引号不闭合 / 孤立引号 |
| `text/html; charset="utf-8"x` | `None` | 闭引号后有垃圾字符 |
| `text/html; xcharset="utf-8"` / `mycharset=utf-8` | `None` | 参数名必须精确等于 charset |
| `text/html; foo="bar; charset=utf-7;"; charset=utf-8` | `'utf-8'` | 引号内 charset 不透明；外部参数正常 |
| `text/html; foo="bar; charset=utf-7"` | `None` | 唯一的 charset 藏在引号里 |
| `text/html; charset=""; charset=utf-8` | `'utf-8'` | 空引号值不是合法标签，跳过继续扫描 |
| `text/html; charset=; charset=utf-8` | `'utf-8'` | 空值同上 |
| `text/html; charset=bogus; charset=utf-8` | `None` | **第一个语法命中的 charset 无法解析即终止，不回退到第二个** |
| `text/html; charset=utf-8; charset=bogus` | `'utf-8'` | 第一个可解析，立即采用 |
| `text/html;charset=utf-8, text/html` | `'utf-8'` | 逗号处截断值，模拟 Fetch 的 MIME 提取 |
| `text/html; charset=utf-8, text/plain; charset=utf-16` | `'utf-8'` | **逗号合并的重复头：第一个 charset 胜**（与 Fetch“最后一个合法 MIME 类型胜”刻意不同，见 `encoding.py:23-26` 注释） |
| `text/plain; charset=utf-16, text/html; charset=utf-8` | `'utf-16-le'` | 同上，第一个头里的 charset 先命中 |
| `text/html; charset=utf-7` | `None` | UTF-7 不在 Encoding Standard，防止 charset 走私 |
| `text/html; charset=base64` / `rot13` / `unicode-escape` | `None` | 非文本/转义 codec 一律不解析 |
| `text/html; charset=latin1` | `'cp1252'` | WHATWG 别名重映射 |
| `None` / `""` / `"   "` / `"text/html"` | `None` | 无头或无 charset 参数 |

注意“空值继续、坏值终止”的不对称：`charset=""` 因标签正则 `[\w-]+` 不匹配空串而被跳过，扫描可以继续；而 `charset=bogus` 语法上是完整参数，进入 `resolve_encoding` 得到 `None` 后整个函数直接返回 `None`。

### 3.2 对最终文本与错误处理的影响

- 头给出**可解析**编码时无条件采用，即使与字节内容矛盾，也不会回退到 meta 或嗅探：
  - UTF-8 字节 `привет…` 配 `charset=cp1251` → 报告 `cp1251`，文本变成 mojibake `'РїСЂРёРІРµС‚'`；
  - Shift-JIS 字节 `82 A0` 配 `charset=utf-8` → 报告 `utf-8`，文本是两个 `'��'`，不抛异常。
- 头标签**不可解析**（`None`）时，等于没有头，流程继续走 meta → UTF-8 校验 → 嗅探。实测 `charset=totally-unknown-xyz` 配合不同 body：ASCII body → `('utf-8', 'plain ascii text')`；`82 A0` → `('cp866', 'Ва')`；`A3` → `('cp1250', 'Ł')`；带 big5 meta 的 body → `('big5hkscs', '恒')`。
- 安全收益：`+ADw-script+AD4-` 这类 UTF-7 走私载荷在头和 meta 中都不被识别为 UTF-7，解码后保持惰性文本（测试 `test_resolve_encoding_utf7_not_smuggled`）。

---

## 4. HTML 内声明：meta 预扫描与 XML 声明

`html_body_declared_encoding()` 委托原生 `declared_encoding()` 做 WHATWG prescan，w3lib 再用 `codec_name()` 转成 Python codec 名；传入 `str` 时先以 UTF-8 `replace` 编码再扫描（`encoding.py:103-106`）。

### 4.1 识别规则实测

| body 片段 | 结果 | 规则要点 |
|---|---|---|
| `<meta charset="utf-8">` | `'utf-8'` | HTML5 写法，属性空白/换行容忍 |
| `<meta http-equiv="content-type" content="text/html;charset=gbk">` | `'gbk'` | pragma 写法 |
| 同一 meta 上 `content` 说 gbk、`charset` 属性说 utf-8 | `'utf-8'` | charset 属性胜，且位置无关 |
| 两个 content 属性 | 第一个 content 生效 | 重复属性第一个胜 |
| `<meta charset="">` | `None` | 空声明不算，扫描继续 |
| `<meta name=description content="…charset=big5…">` | `None` | 无 content-type pragma，content 里的 charset 是普通文本 |
| `<meta http-equiv="refresh" content="0;url=?charset=big5">` | `None` | 非 content-type pragma |
| `<!-- <meta charset="utf-7"> -->` 后接正常 meta | 后者生效 | 注释内标签隐藏；注释在首个 `-->`/`--!>` 结束 |
| 未闭合注释 `<!-- <meta charset="big5">` | `None` | 注释吞掉其后全部内容 |
| `<img alt="<meta charset=big5>">` | `None` | 属性值内的标签不是标签 |
| `<head></head>` 或 `<body>` 之后紧跟 meta | 仍可识别 | body 后首个标签可以是 meta；但其后一旦出现普通元素，再后面的 meta 失效（详见 4.2） |
| `<title>…<meta …>` / `<script>…<meta …>` | `None` | title/script 是原始文本元素，内部暂停扫描；闭合后恢复（`</title>` 后的 meta 正常识别） |
| `<?xml version="1.0" encoding="shift_jis"?>` | `'cp932'` | XML 声明 |
| 前面加哪怕一个空格/换行的 XML 声明 | `None` | **XML 声明只在绝对偏移 0 识别** |
| `<meta charset="utf-16">` | `'utf-8'` | 声明 UTF-16 按规范改写为 UTF-8（这类 ASCII 页面本就按 UTF-8 读） |
| `<meta charset=x-user-defined>` | `'cp1252'` | meta 中的 x-user-defined 改写为 windows-1252 |
| `<meta charset="utf-7">` | `None` | 不解析，继续后续步骤 |
| 两个 meta 声明不同编码 | **第一个胜**（big5 在前 → `big5hkscs`） | 与重复头参数同构 |

### 4.2 预扫描的预算与提前终止（实测，两条独立机制）

原生 prescan 复刻 WHATWG 算法的 1024 字节预算，但预算的消耗方式与结构标签相关，实测行为如下：

**机制一：遇到结构性标签立即终止扫描（无论已扫多少字节）。**

| 结构 | 紧跟 meta | 标签后填充 2000 字节再放 meta |
|---|---|---|
| `</head>` | 识别 | `None` |
| `<body>` / `<body >` / `<body\n>` / 带属性的 `<body class="x">` | 识别 | `None` |
| `<div>` / `<p>`（body 内普通元素） | 识别 | `None` |
| `<html>` / `<head>` / `</body>` | 识别 | **仍识别**（不是终止标签） |

即 WHATWG prescan 的终止条件是进入 body 后遇到非 meta 类标签：一旦在 `<body>`（或 `</head>`）之后开始解析普通元素，后续 meta 全部不可见。这正是测试中 `'<head></head><body><meta charset="utf-8">'` 仍返回 utf-8 的原因——meta 是 body 后的第一个标签，尚未遇到普通元素。

**机制二：1024 字节 prescan 预算按文档绝对偏移计量。**

`<body>` 位于偏移 0（标签占 6 字节）时，meta 起始偏移 1023 可见、1024 不可见（实测边界）；`</head>` 后同理。即预算计量的是从文档开头消费的字节总数，终止标签本身计入预算；预算用尽后无论后续是否还有 meta 都不再可见。

**机制三：文档从未出现终止标签时，meta 扫描不设窗口上限。**

纯填充字节（`x`）后接 meta，即使 meta 起始于偏移 10 万，`declared_encoding` 与端到端 `detect_encoding` 仍返回 Big5；`<meta http-equiv>` pragma 同理。也就是说规范的 1024 上限在本实现中只在“已经进入 body/head 结束的结构化文档”里生效，无结构的扁平字节流会被一直扫描到 EOF。

**XML 声行走独立路径：只在绝对偏移 0 识别。** 前面加哪怕一个空格/换行即返回 `None`（见 4.1 表），与 meta 的窗口机制无关；偏移 0 之外的 `<?xml encoding?>` 永不参与。

迁移含义：旧 w3lib“遇到 `<body>` 标签本身就停止”的行为与新版不同——新版允许 body 后的第一个 meta 生效，但 body 内一旦出现普通元素，之后的 meta 与旧版一样不可见；而对没有 head/body 结构的畸形页面，新版扫描范围远大于旧版和规范字面要求。

### 4.3 非 ASCII 字节与扫描的关系

非 ASCII 字节出现在 meta 之前不影响标记识别（标记本身是 ASCII 安全的），但这些字节最终按选定编码解码，可能产生 `U+FFFD`，实测 `b'\xa3<meta charset=big5>'` → `'�<meta charset=big5>'`（big5 下 `0xA3` 是引导字节，单字节非法）。

---

## 5. UTF-8 有效性闸门

前四层都没有给出编码时，先对**整个 body** 做严格 UTF-8 校验（原生 `autodetect_encoding` 的第一步）：

| body | 是否合法 UTF-8 | 自动判定 |
|---|---|---|
| `b""`、纯 ASCII | 是（空体也算） | `UTF-8` |
| `caf\xc3\xa9`、`e3 81 82`（あ）、`f0 9f 98 80`（😀） | 是 | `UTF-8` |
| `\x1b$Babc`（ISO-2022-JP 转义序列，字节全 ASCII） | 是 | `UTF-8`（不会猜成 ISO-2022-JP） |
| `#\xa3`、`\x80`、`\xff` | 否 | 进入字节+TLD 嗅探 |
| `\xc0\x80`（overlong NUL）、`\xed\xa0\x80`（代理区）、`\xf4\x90\x80\x80`（超 U+10FFFF） | 否 | 进入嗅探 |
| 10 万 ASCII + 末尾一个 `\xff` | 否 | 整体进入嗅探（一个坏字节否决整页 UTF-8） |

要点：

1. 该闸门只在“无 BOM、无可用头、无可用 meta”时才到达；显式 `charset=utf-8` 头不做整页校验，坏字节只在解码阶段变 `U+FFFD`（`e3 ab` → 一个 `�`，截断的多字节序列同理），编码仍报告 `utf-8`。
2. 判定是全有或全无：没有“部分 UTF-8”，只要一个非法字节就落到嗅探器，结果可能是任何传统编码。
3. 空体与纯 ASCII 体恒为 UTF-8，因此 `html_to_unicode(None, b"") == ('utf-8', '')`。

---

## 6. 字节特征 + 顶级域猜测（Firefox 模型）

### 6.1 TLD 映射表（实测，body 固定为 `b'\x82\xa0'`）

| 猜测编码（WHATWG / Python） | 触发 TLD |
|---|---|
| Shift_JIS / `cp932` | `jp`（含 `co.jp` 这类二级域，取最后一个标签） |
| GBK / `gbk` | `cn`、`tw`、`sg` |
| EUC-KR / `cp949` | `kr` |
| Big5 / `big5hkscs` | `hk` |
| ISO-8859-7 / `iso8859-7` | `gr` |
| windows-1255 | `il` |
| windows-1256 / `cp1256` | `eg`、`sa`、`ae`、`pk`、`ir`、`iq`、`jo`、`lb`、`sy`、`ye`、`ly`、`tn`、`dz`、`ma` |
| IBM866 / `cp866`（默认） | 其它一切：`com/org/net/…`、`ru/su/ua/pl/…`、无 url、无法解析的 host |

URL 解析实测边界：

- 大小写不敏感：`http://example.JP/` 生效；但 `http://JP.EXAMPLE.COM/` 的 TLD 是 `com` → 默认 `IBM866`。
- 取 host 最后一个点号标签，容忍端口/路径/查询：`example.jp:8080/a?b=1`、无尾斜杠 `http://example.jp` 均生效。
- 无 scheme 的 `example.jp`、`localhost`、IP 字面量 `1.2.3.4`/`[2001:db8::1]`、IDN TLD `example.中国`、`file:///`、空串 → 全部落默认 `IBM866`（即调用方必须传完整绝对 URL，TLD 猜测才有机会工作）。

### 6.2 TLD 与字节信号的关系：TLD 是硬覆盖

同一字节在不同 TLD 下给出不同编码，说明 TLD 不是弱提示而是检测器的区域分支选择器（Firefox 旧 `nsUniversalDetector` 的分区域 observer 结构）：

| 字节 | 无 url | `.jp` | `.cn` | `.kr` | `.ru` | `.gr` | `.il` | `.eg` | `.tr` | `.hk` |
|---|---|---|---|---|---|---|---|---|---|---|
| `82 A0` | IBM866 | Shift_JIS | GBK | EUC-KR | IBM866 | ISO-8859-7 | cp1255 | cp1256 | IBM866 | Big5 |
| `A3` | cp1250 | Shift_JIS | cp1250 | cp1250 | cp1251 | ISO-8859-7 | cp1255 | cp1256 | cp1254 | cp1250 |
| `80` | cp1252 | Shift_JIS | GBK | EUC-KR | cp1251 | ISO-8859-7 | cp1255 | cp1256 | cp1254 | Big5 |
| `A1 A1` | ISO-8859-2 | EUC-JP | GBK | EUC-KR | IBM866 | ISO-8859-7 | cp1255 | cp1256 | cp1254 | GBK |
| `81 40` | Shift_JIS | Shift_JIS | GBK | EUC-KR | cp1251 | Shift_JIS | Shift_JIS | cp1256 | Shift_JIS | GBK |
| `C7 FD` | cp1254 | EUC-JP | GBK | EUC-KR | cp1251 | ISO-8859-7 | cp1255 | cp1256 | cp1254 | GBK |

无 TLD 时的纯字节默认分支（实测样例）：`A3`→windows-1250；`80/FF/C0 80/…`→windows-1252；`82 A0`→IBM866；`A1 A1`→ISO-8859-2；`81 40`→Shift_JIS；`C7 FD`→windows-1254；`F4 90 80 80`→IBM866。这些是各区域 observer 对模糊字节的默认裁决，**调用方无法从 w3lib 公共 API 拿到确定映射表，只能以实测为准**——这也是报告必须用字节样例而非文字描述覆盖该层的原因。

对页面文本的影响：嗅探结果直接决定解码 codec，同一份 `82 A0` 在 `.com` 下是西里尔文 `'Ва'`（cp866），在 `.jp` 下是日文 `'あ'`（cp932）；没有声明的遗留页面文本随 url 变化而变化。

---

## 7. 已弃用默认编码与自动探测回调的责任边界

### 7.1 三个弃用面（2.6.0，`encoding.py:109-140, 280-295`）

1. `html_to_unicode(..., default_encoding=..., auto_detect_fun=...)`：任一非默认即发 `DeprecationWarning`，并切换到**遗留分支**。
2. `w3lib.encoding.DEFAULT_ENCODING_TRANSLATION`：模块级 `__getattr__` 懒加载，访问即警告。实测全仓库 grep 确认该表（ascii→cp1252、gbk→gb18030 等 19 项）**已不被任何解码路径引用**，纯粹是为旧代码 `from w3lib.encoding import DEFAULT_ENCODING_TRANSLATION` 保留的数据属性；它的映射内容与新 WHATWG 标签表也不一致（例如表里 `gbk→gb18030`，而新流程 `gbk→gbk`）。

### 7.2 两条流程的责任划分

| 环节 | 新流程（无弃用参数，可带 `url`） | 遗留分支（出现 default/callback 之一） |
|---|---|---|
| BOM / BOM-less UTF-16 | 原生 `bom_encoding`，最高优先、剥离 BOM | **相同**：BOM 仍在分支之前处理，回调无法覆盖 |
| 头 charset | w3lib 正则 + WHATWG 解析 | 相同 |
| meta 声明 | 原生 prescan | 仅当 `enc is None` 时调用同一个 `html_body_declared_encoding`（即新 prescan，旧的 `<body>` 截断等老行为不会因遗留参数复活） |
| 自动探测 | 原生 UTF-8 闸门 + 字节/TLD 嗅探，`url` 生效 | **不执行**；改调 Python 回调 `auto_detect_fun(body)`，`url` 参数被完全忽略 |
| 最终兜底 | 嗅探器必有返回，不会到 UTF-8 硬编码 | `default_encoding or "utf8"` 字面量 |
| 标签规范化 | 全部经 WHATWG 表，返回 canonical Python 名（`utf-8`、`cp1252`） | 回调/默认值**原样作为返回编码名**，只在解码时经 `to_unicode` 按 WHATWG 解 |
| 异常保证 | 永不抛编码异常 | 回调/默认给出未知标签时，`to_unicode` 内 `decode` 抛 `LookupError` 且 fallback 的 `bytes.decode` 同样找不到 codec → **LookupError 会向调用方传播** |

实测对照（同一 body `b'\x82\xa0'`，url=`http://example.jp/`）：

```python
html_to_unicode(None, b"\x82\xa0", url="http://example.jp/")
#   新流程 -> ('cp932', 'あ')
html_to_unicode(None, b"\x82\xa0", auto_detect_fun=lambda b: None, url="http://example.jp/")
#   遗留分支（警告）-> ('utf8', '��')   # TLD 被绕过，utf8 拼写也未规范化
```

遗留分支内部顺序实测（每一层缺失才落到下一层）：**头 → meta → 回调 → default_encoding → 字面量 `"utf8"`**。

- 回调返回 `None` 才继续；返回 `'latin1'` 时返回名保持 `'latin1'`（不是 `cp1252`），但字节按 WHATWG 解码（`A3 → £`）。
- `default_encoding='ascii'` 同理返回名 `'ascii'`；`default_encoding='totally-unknown-xyz'` 或回调返回该串 → 抛 `LookupError: unknown encoding`。
- 每次进入遗留分支发一条 `DeprecationWarning`（两个参数同时给也只发一条）。

### 7.3 与 `html_encoding()` 的不一致

`html_encoding()` 没有遗留参数，**永远走新流程**。因此同一份 body 在“带回调的 `html_to_unicode`”与“`html_encoding`”之间可能返回不同编码名（实测 `'ascii'` vs `'utf-8'`、`'latin1'` vs 嗅探结果）。旧代码若用 `html_encoding()` 预判再用带回调的 `html_to_unicode()` 解码，会出现预判与实际解码不符——迁移时必须先去掉弃用参数。

---

## 8. 结果不一致时如何保持旧调用兼容

### 8.1 现状已经提供的兼容机制

1. **弃用参数不删、语义冻结**：传入即走独立遗留分支并警告，新检测引擎的行为变更（TLD 嗅探、prescan 改写、标签表）被限制在新分支内，不回灌旧调用。需要旧行为的调用方继续传 `default_encoding`/`auto_detect_fun` 即可保持原有四层顺序与 `"utf8"` 兜底。
2. **BOM 与头提取两分支共用**：这两层是无争议的规范化逻辑，共用保证安全修复（UTF-7 拒绝、引号参数）对所有调用方同时生效。
3. **meta 层共用新 prescan 是有意的行为统一**：遗留分支不保留旧正则扫描器，旧调用方也获得注释/属性感知修复；若旧代码依赖“`<body>` 后停止”“拼错的 http-equiv 生效”等老行为，这是已知破坏性变更（NEWS 2.6.0 明确列出），无兼容开关。
4. **`DEFAULT_ENCODING_TRANSLATION` 表保留可读**：仅属性访问兼容，不参与流程；旧代码读表做自定义映射仍可工作（带警告）。
5. **codec 注册副作用**：导入 `w3lib.encoding` 即通过 frostwork 注册 `x-user-defined` 与 `replacement` 两个 Python codec，使旧代码用返回名直接 `bytes.decode(name)` 也不失败。
6. **`to_unicode()` 双保险**：先试 WHATWG `decode`，`LookupError` 时退回标准库 `bytes.decode(encoding, "replace")`；但未知标签两者都找不到时仍抛 `LookupError`，调用方不能假设“任何字符串都能解”。

### 8.2 迁移建议（消除不一致的正确方向）

- 去掉 `default_encoding`/`auto_detect_fun`，改为传 `url`：把旧回调的知识前置到响应获取层（修正头/meta），或在 w3lib 之外先判定再显式传正确的 Content-Type。
- 编码名比较改用 `resolve_encoding()`/`codec_name()` 规范化后再比，不要依赖返回字符串字面量（新流程返回 `utf-8` 而非 `utf8`、`cp1252` 而非 `latin1`）。
- 需要“默认 UTF-8、不嗅探”的旧语义时，不要依赖无声明 body 的自动结果：显式给 `Content-Type: text/html; charset=utf-8` 头，坏字节会稳定地变成 `U+FFFD` 而不是被猜成别的编码。
- 任何情况下都不要按返回名假设解码无异常：遗留分支的未知标签会抛 `LookupError`，新流程则永不抛（不可解析标签被忽略后必有下游裁决）。

---

## 9. 每个结论对最终页面文本与错误处理的影响汇总

| 结论 | 对页面文本的影响 | 对错误处理/返回值的影响 |
|---|---|---|
| BOM 最高优先并剥离 | BOM 不残留为 `U+FEFF`；头/meta/url 全部失效 | UTF-32LE 被按 UTF-16LE 解，文本插 `\x00`，但不报错 |
| 头 charset 可解析即锁定 | 与字节矛盾时产生 mojibake（如 cp1251 解 UTF-8） | 非法字节→`U+FFFD`；函数不抛异常 |
| 头标签不可解析则透明降级 | 页面改由 meta/UTF-8/嗅探决定文本 | `http_content_type_encoding` 返回 `None`，无警告无异常 |
| 引号参数/重复头取第一个语法命中 | 逗号合并头按第一个 charset 出文本 | 首个坏标签使整头返回 `None`（空值例外，跳过继续） |
| UTF-7/转义 codec 拒绝 | 走私载荷保持惰性文本，不变成可执行标记 | 标签返回 `None`，流程继续 |
| meta 第一个声明胜、title/script 内暂停 | 头部区域内的首个有效声明决定全文 | 未闭合注释吞掉后续声明 → 落到 UTF-8/嗅探 |
| meta prescan：结构标签提前终止 + 1024 预算；扁平无结构文档不设窗口；XML 声明限偏移 0 | body 后首个 meta 仍能决定全文，body 内普通元素之后的 meta 失效；无结构文档中很靠后的 meta 也能改写整页解码 | 与旧 w3lib“见 body 即停”和规范字面 1024 上限均有实测差异，见 4.2 |
| UTF-8 全页校验是全有全无 | 一个坏字节使整页改走传统编码，可能全面乱码 | 不抛异常；纯 ASCII/空体稳定走 UTF-8 |
| TLD 硬覆盖字节嗅探 | 同一字节随域名 TLD 变成不同文字 | 无 url / 非绝对 URL 时静默落 IBM866 默认分支 |
| WHATWG 解码替换错误 | 所有非法输入统一 `U+FFFD`（含 replacement 编码整页变一个 `�`） | 新流程零异常保证；`replacement`/`x-user-defined` 有已注册 codec |
| 遗留分支绕过 TLD 嗅探 | 回调/默认值决定文本，`url` 失效 | 发 `DeprecationWarning`；未知标签抛 `LookupError`；返回名不 canonical（`utf8`/`latin1`/`ascii` 原样） |
| 弃用表脱离流程 | 表内映射（如 gbk→gb18030）不再影响任何页面 | 仅访问时警告；新流程实际为 gbk→`gbk` |
| `str` 误入 `html_to_unicode` | —— | 原生层要求 bytes，抛 `TypeError: string argument without an encoding`；`html_body_declared_encoding` 则接受 str（内部 UTF-8 重编码） |

---

## 10. 附：复现脚本与测试入口

- 探测脚本（独立目录，全部以 `/private/tmp/npvenv/bin/python -I /tmp/w3lib-encoding-probe/probeN.py` 运行）：
  - `probe.py`：BOM、头引号/重复/逗号、meta、端到端优先级、UTF-8 闸门、TLD 表、遗留分支、错误处理
  - `probe2.py`：未知标签降级、完整 TLD 扫描、URL 边界、重复 charset、BOM-less UTF-16
  - `probe3.py`：引号坏值终止、遗留路径 LookupError、BOM 与回调关系、警告断言
  - `probe4.py`：prescan 窗口、重复 meta、str 入参、返回名拼写差异、BOM 剥离细节
  - `probe5.py`：meta/XML 窗口对比、扫描停止标签、嗅探器字节裁决
  - `probe6.py`：XML 偏移 0 限制、UTF-8 合法性边界、FFFD 计数
  - `probe7.py`：title/script 暂停恢复、TLD 对字节信号的硬覆盖矩阵
  - `probe8.py` / `probe9.py`：prescan 提前终止标签、1024 字节预算边界、无结构文档的无界扫描复核
- 官方测试：`python -I -m pytest tests/test_encoding.py -q` → 97 passed，可作为本报告所有结论的回归基线。
- 源码锚点：`w3lib/encoding.py:27-78`（头提取双正则路径）、`:109-140`（弃用表与懒警告）、`:163-186`（BOM）、`:206-219`（`html_encoding`）、`:222-295`（两条分支的总分发）；`frostwork/encoding.py:84-118`（原生检测函数封装）、`:33-56`（WHATWG→Python codec 映射）。
