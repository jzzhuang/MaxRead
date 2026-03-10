# Transform

Markdown → Feishu docx transformation: parse `.md` into block types (headings, text, equations, **bold**/ *italic*, bullet/ordered lists, tables) and optionally create Feishu cloud docs.

## Layout

- **`constants.py`** – Feishu docx block type IDs (text, heading1–3, equation, bullet, ordered, table, code)
- **`inline.py`** – `parse_inline(text)` → segments with bold/italic for styled text runs
- **`parser.py`** – `parse_md_to_blocks(md)` → list of `(block_type, content)` (tables: separator row skipped; content is string or `{"rows": [[cell,...], ...]}` for tables)
- **`feishu_doc.py`** – `build_block()`, `create_summary_doc()`, `doc_url()` (Feishu API; tables created as native table blocks and cells filled via batch update)

## Module testing (CLI)

Run from project root (`/home/tianze/MaxRead`):

```bash
# Local: parse a fixed .md file, write blocks to JSON, print output path
python -m transform path/to/summarize.md
# → prints e.g. /home/tianze/MaxRead/transform_out/summarize_blocks.json

python -m transform --md path/to/summarize.md --out-dir ./out

# With Feishu: create cloud doc and print doc URL (needs feishu/.env)
python -m transform --md path/to/summarize.md --feishu --title "My Summary"
# → prints e.g. https://open.feishu.cn/docx/xxx
```

Output in local mode: one JSON file per run under `--out-dir` (default `transform_out/`), with `block_type` names and `content` for each block.
