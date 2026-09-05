# OpenScite

[English](README.md) | 繁體中文

> 給 agent 一篇學術論文 PDF，取得後續論文如何支持、對比或提及它的排序與證據連結地圖。

OpenScite 是一個用於分析引用關係的開源 [Agent Skill](https://agentskills.io/)。它會從 PDF 識別目標論文、找出引用該論文的作品、優先處理最可能包含有用證據的引用論文、取得合法可存取的全文、擷取精確的引用段落，並以使用者的語言產生報告。

標準流程以本機與免金鑰為優先：[OpenAlex](https://openalex.org/) 提供引用圖譜與中繼資料，最新的 [Anydoc](https://github.com/firecrawl/anydoc) CLI 則在本機解析文字型 PDF。標準 runner 不需要也不使用 Semantic Scholar。

## 為什麼需要另一個引用工具？

引用次數只能告訴你後續論文引用過某篇作品，無法告訴你它們如何討論該作品。逐篇手動搜尋所有引用論文很慢，而只依引用次數排序，容易讓高引用但僅作背景提及的文章掩蓋複製研究、零結果與直接批評。

OpenScite 使用兩階段流程：

1. 根據目標論文的主張、標題／摘要證據訊號、論文引用次數、出版年份，以及 OpenAlex 的期刊層級引用代理指標，排序引用該論文的作品。
2. 只有在引用論文全文中找到並綁定目標論文的引用段落後，才判定引用立場。

最終標籤套用在個別引用段落，而不是整篇論文：

| 標籤 | 意義 |
| --- | --- |
| `supporting` | 報告與目標主張一致或複製目標結果的證據。 |
| `contrasting` | 報告衝突證據、複製失敗、零結果、重新分析或實質限制。 |
| `mentioning` | 將目標論文作為背景或出處使用，但沒有評估其主張。 |
| `unknown` | 全文、目標綁定或證據不足，無法可靠判定。 |

## 流程

```text
目標 PDF
  -> 識別論文並擷取其主張
  -> 使用 OpenAlex 找出引用該論文的作品
  -> 從摘要優先找出可能對比／支持目標的候選者
  -> 選取前 N 篇論文
  -> 取得開放取用全文，或選擇使用授權的瀏覽器 session，
     或要求使用者提供檔案
  -> 解析並綁定精確的引用段落
  -> 判定每個段落的立場並產生 report.md
```

Agent 會以可恢復的方式執行流程。若使用者沒有提供 shortlist 大小，才會詢問一次，預設建議 `N=20`，並接受保留原始檔名的引用論文檔案。如果直接取得開放取用全文失敗，Agent 可以詢問是否使用使用者目前的校網／VPN 瀏覽器 session。獲得授權的瀏覽器下載會以最多四個分頁為一批平行執行；使用者也可以選擇改用 `fulltext-requests.md` 中的 DOI 或文章連結自行下載。

已完成的中繼資料、PDF 轉換、摘要分流與立場標註都會快取。使用相同參數重新執行時，只會處理缺少或無效的工作。

## 安裝

建議使用 [Skills CLI](https://skills.sh/docs/cli) 將 OpenScite 安裝到使用者範圍：

```bash
npx skills add MO7YW4NG/openscite -g
```

### 從本機 clone 開發

Repository 根目錄就是 skill package，因此以下指令應列出 `openscite`：

```bash
npx skills add . --list
```

在這個 source repository 內不要將 `.` 安裝到 project scope，否則目的地會巢狀在自己的 source 目錄中。開發時請安裝到 global scope，或從另一個 consumer project 安裝：

```bash
npx skills add . -g
```

## 使用方式

附加或引用一篇學術論文 PDF，並明確要求 Agent 使用 OpenScite。例如：

```text
Use OpenScite to analyze ./paper.pdf. Prioritize 20 citing papers that are
most likely to support or contrast with its main empirical claims.
```

可以省略數量，讓 Agent 自行詢問一次；也可以選擇其他排序模式：

| 模式 | 適用情境 | 排序方式 |
| --- | --- | --- |
| `stance_first` | 尋找複製研究、衝突結果與直接評估 | 預設模式。優先使用摘要證據訊號，將中繼資料作為平手時的排序依據，並保留摘要缺失或不確定文章的探索區。 |
| `influence_first` | 掃描最具影響力的引用文獻 | 依論文引用次數、OpenAlex Source 2-year mean citedness 與出版年份排序。 |

Source metric 是引用代理指標，不是 Clarivate Journal Impact Factor。OpenScite 不會將它報告為 JIF 或 Impact Factor。

## 輸出

執行目錄預設為 `artifacts/openscite/<paper-name>/`，包含：

| 檔案 | 用途 |
| --- | --- |
| `report.md` | 以使用者語言撰寫的精簡結果，按引用論文與立場分組。 |
| `fulltext-requests.md` | 選定論文缺少全文時的合法下載連結。 |
| `citations.json` | 段落層級的證據、不可變的上下文雜湊、標籤、信心值與理由。 |
| `citing-works.json` | 選定引用論文及其取得／上下文狀態。 |
| `run.json` | 精簡的流程狀態、覆蓋率統計與可恢復階段資訊。 |

標題與引用段落會保留原文。Provider 診斷資訊、parser 內部細節與排序分數不會放入給使用者閱讀的報告。

## 免金鑰與本機優先

- 基本 OpenAlex 查詢不需要 API key，但匿名使用的每日額度小於免費驗證帳戶。詳見目前的 [OpenAlex authentication policy](https://help.openalex.org/api/authentication/)。
- Anydoc 會使用目前的 `@firecrawl/anydoc` 版本，並以 `--ocr reject` 在本機執行。首次使用時，`npx` 會下載平台 binary；一般 PDF 內容不會傳送到 Firecrawl。
- OpenScite 只下載合法的開放取用候選檔案，並驗證回應確實是 PDF。不會繞過付費牆，也不會使用需要付費的 OpenAlex 全文端點。
- 出版商全文的瀏覽器下載需要使用者選擇同意，並使用使用者現有的登入 session 或校網 VPN；每批最多四個分頁。Agent 不會要求或輸入帳密，受阻的論文會保留給使用者處理。
- 不會自動上傳掃描版或純影像 PDF 進行 OCR。Hosted OCR 需要使用者明確同意；在免金鑰本機模式中，優先使用可搜尋 PDF、HTML／JATS 版本或本機 OCR。
- 使用者提供的引用論文會保留檔名，並依 DOI／標題進行比對；不會重新命名或搬移。

## 需求

Skills CLI 只會安裝 skill 檔案，不會安裝以下 runtime 依賴：

| 需求 | 用途 |
| --- | --- |
| Python 3.11+ | 可恢復 runner 的必要環境；runtime code 使用標準函式庫。 |
| 網際網路 | 取得 OpenAlex 中繼資料與開放取用全文。 |
| Node.js 20+ 與 `npx` | 建議的預設 PDF 路徑；執行最新的 Anydoc package。 |
| Poppler（`pdftotext`、`pdfinfo`） | 可選 fallback；需要可靠的頁碼感知擷取時使用。 |
| Browser automation | 可選；透過使用者現有的校網／VPN session 取得授權的出版商 PDF。 |

不需要永久性的 parser API key。

## 限制

- 摘要分流只能預測哪些論文值得開啟，不會直接決定最終立場。
- 即使論文可讀，如果無法可靠地將文中引用綁定到目標論文，結果仍可能是 `unknown`。
- 全文覆蓋率取決於合法的開放取用來源、授權的瀏覽器存取權或使用者提供的檔案。
- 一篇引用論文可能包含多個不同標籤的引用段落。
- 需要精確頁碼定位時，請使用 `--require-page-aware`；該選項會刻意繞過 Anydoc，改用 Poppler 路徑。

## 開發

Agent-facing skill 會呼叫 deterministic runner，不會產生臨時 pipeline script：

```bash
python scripts/openscite.py prepare ./paper.pdf --n 20 --language zh-TW
python scripts/openscite.py status --run-dir ./artifacts/openscite/<run>
python scripts/openscite.py finalize --run-dir ./artifacts/openscite/<run>
```

`prepare` 可能會因為需要擷取目標主張、分流摘要、要求缺少全文或分類待處理引用段落而暫停，並提供下一步。完成該步驟後，使用相同指令即可從快取成果繼續執行。

執行回歸測試：

```bash
python -m unittest discover -s tests -v
```

## 目錄結構

```text
openscite/
├── SKILL.md                 # Agent Skill 的主要指示
├── agents/openai.yaml       # OpenAI/Codex skill 中繼資料
├── scripts/
│   ├── openscite.py         # 唯一 CLI 入口
│   ├── openscite_core.py    # 可恢復流程與驗證
│   ├── documents.py         # 本機解析、比對與擷取
│   └── render_report.py     # 精簡 Markdown 報告產生器
├── references/              # 排序、標註、provider 與 artifact contract
└── tests/                   # deterministic regression suite
```

## 授權

[MIT](LICENSE)
