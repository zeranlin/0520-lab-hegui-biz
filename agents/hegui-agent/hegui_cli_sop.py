#!/usr/bin/env python3
"""SOP-governed government procurement compliance review executor."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


EXECUTOR_NAME = "hegui_cli_sop.py"
SOP_DOC_REL = "docs/hegui_cli_review_sop.md"
WIKI_FEEDBACK_DOC_REL = "docs/hegui_cli_wiki_driven_issues.md"


def read_simple_yaml_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    prefix = f"{key}:"
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value.strip("\"'")
    return None


def read_auth_key(config_dir: Path) -> str:
    auth_file = config_dir / "auth.json"
    if not auth_file.is_file():
        return ""
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    return str(data.get("OPENAI_API_KEY") or "")


def read_toml_string(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return ""


def local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone()


def sop_preflight(biz_home: Path, wiki_home: Path, config_dir: Path) -> list[str]:
    """Check SOP governance inputs without adding business review knowledge."""
    issues: list[str] = []
    required_files = [
        biz_home / SOP_DOC_REL,
        biz_home / WIKI_FEEDBACK_DOC_REL,
        biz_home / "config/hegui.yaml",
        config_dir / "config.toml",
        config_dir / "auth.json",
    ]
    for path in required_files:
        if not path.is_file():
            issues.append(f"missing required SOP/runtime file: {path.relative_to(biz_home).as_posix()}")
    if not wiki_home.is_dir():
        issues.append("wiki home not found")
    return issues


DOCX_NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}


def docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "t" and node.text:
            parts.append(node.text)
        elif tag == "tab":
            parts.append("\t")
        elif tag == "br":
            parts.append("\n")
    return html.unescape("".join(parts)).strip()


def docx_table_text(table: ElementTree.Element) -> str:
    rows: list[str] = []
    for row in table.findall("./w:tr", DOCX_NAMESPACES):
        cells: list[str] = []
        for cell in row.findall("./w:tc", DOCX_NAMESPACES):
            cell_parts = [
                text
                for paragraph in cell.findall(".//w:p", DOCX_NAMESPACES)
                if (text := docx_paragraph_text(paragraph))
            ]
            cells.append(" ".join(cell_parts))
        if any(cells):
            rows.append("\t".join(cells))
    return "\n".join(rows).strip()


def extract_docx_text(target_path: Path) -> str:
    blocks: list[str] = []
    with zipfile.ZipFile(target_path) as docx:
        document_xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    body = root.find("w:body", DOCX_NAMESPACES)
    if body is None:
        return ""
    for child in body:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = docx_paragraph_text(child)
            if text:
                blocks.append(text)
        elif tag == "tbl":
            text = docx_table_text(child)
            if text:
                blocks.append(text)
    return "\n".join(blocks)


def extract_pdf_text(target_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(target_path), "-"],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("PDF text extraction requires `pdftotext` in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"PDF text extraction failed: {exc.stderr.strip()}") from exc
    return result.stdout


def extract_plain_docx_text(target_path: Path) -> str:
    paragraphs: list[str] = []
    with zipfile.ZipFile(target_path) as docx:
        document_xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(document_xml)
    for paragraph in root.findall(".//w:p", DOCX_NAMESPACES):
        parts: list[str] = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag == "tab":
                parts.append("\t")
            elif tag == "br":
                parts.append("\n")
        text = html.unescape("".join(parts)).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_file_text(target_path: Path) -> str:
    suffix = target_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(target_path)
    if suffix == ".docx":
        return extract_docx_text(target_path)
    if suffix == ".doc":
        raise RuntimeError("Legacy .doc is not supported cross-platform. Please convert it to .docx first.")
    if suffix in {".txt", ".md"}:
        return target_path.read_text(encoding="utf-8")
    raise ValueError(f"unsupported file type: {suffix}")


def line_number_text(text: str) -> str:
    return "\n".join(f"{index:04d}: {line}" for index, line in enumerate(text.splitlines(), start=1))


def split_numbered_text(numbered_text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for line in numbered_text.splitlines():
        line_chars = len(line) + 1
        if current and current_chars + line_chars > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_chars
    if current:
        chunks.append("\n".join(current))
    return chunks


def count_risks(report: str) -> int:
    return len(re.findall(r"^###\s+风险\s*\d+\s*[.．、：:]", report, flags=re.MULTILINE))


def output_category(target: str) -> str:
    parts = Path(target).parts
    if len(parts) >= 2 and parts[0] == "raw":
        return parts[1]
    return "通用"


def make_run_output_dir(biz_home: Path, category: str, now: datetime) -> tuple[Path, str]:
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    base_rel = Path("outputs") / category / timestamp
    output_dir = biz_home / base_rel
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=False)
        return output_dir, base_rel.as_posix()

    for index in range(1, 100):
        rel = Path("outputs") / category / f"{timestamp}-{index:02d}"
        output_dir = biz_home / rel
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=False)
            return output_dir, rel.as_posix()
    raise RuntimeError(f"cannot allocate output directory for {timestamp}")


def safe_filename_part(text: str, max_chars: int = 40) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "-", text.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return (cleaned or "未命名")[:max_chars]


ENTRY_GUIDE = "wiki/00-入口/外部执行主体招标文件审查指引.md"


def normalize_wiki_ref(ref: str) -> str | None:
    ref = ref.split("|", 1)[0].split("#", 1)[0].strip()
    if not ref:
        return None
    if ref == "wiki/index":
        return "wiki/index.md"
    if ref.startswith("wiki/"):
        return ref if ref.endswith(".md") else f"{ref}.md"
    if ref == "AGENTS.md":
        return ref
    return None


def extract_wiki_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"\[\[([^\]]+)\]\]", text):
        ref = normalize_wiki_ref(match.group(1))
        if ref:
            refs.append(ref)
    for match in re.finditer(r"`((?:AGENTS\.md|wiki/[^`]+?)(?:\.md)?)`", text):
        ref = normalize_wiki_ref(match.group(1))
        if ref:
            refs.append(ref)
    return refs


def route_line_is_enabled(line: str) -> bool:
    positive = ("已调用", "已启用", "启用", "必读")
    negative = ("候选待确认", "待确认", "不适用", "未调用", "未启用", "不调用", "不启用")
    if any(term in line for term in negative):
        return False
    cells = [strip_markdown_cell(cell) for cell in line.strip().strip("|").split("|")]
    if len(cells) >= 9:
        is_required = cells[-2]
        status = cells[-1]
        if is_required == "是" and status in {"已读取", "已调用", "已启用", "启用"}:
            return True
        if status in {"已调用", "已启用", "启用"}:
            return True
    return any(term in line for term in positive)


def extract_enabled_wiki_refs_from_route(route: str) -> list[str]:
    refs: list[str] = []
    for line in route.splitlines():
        if "|" not in line or not route_line_is_enabled(line):
            continue
        for ref in extract_wiki_refs(line):
            if ref not in refs:
                refs.append(ref)
    return refs


def is_allowed_knowledge_page(rel: str) -> bool:
    if rel in {"AGENTS.md", "wiki/index.md", ENTRY_GUIDE}:
        return True
    allowed_prefixes = (
        "wiki/10-法规依据/",
        "wiki/15-行业基础/",
        "wiki/20-知识点/",
        "wiki/25-风险审查点/",
        "wiki/30-风险库/",
        "wiki/40-审查工作台/",
        "wiki/60-提示词/",
        "wiki/70-审查协议/",
        "wiki/90-模板/",
    )
    return rel.startswith(allowed_prefixes)


def collect_entry_driven_knowledge(wiki_home: Path, max_pages: int = 180) -> tuple[str, list[str]]:
    queue = ["AGENTS.md", "wiki/index.md", ENTRY_GUIDE]
    visited: set[str] = set()
    ordered: list[str] = []
    chunks: list[str] = []

    while queue and len(ordered) < max_pages:
        rel = queue.pop(0)
        if rel in visited:
            continue
        visited.add(rel)
        if not is_allowed_knowledge_page(rel):
            continue
        path = wiki_home / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        ordered.append(rel)
        chunks.append(f"\n\n# {rel}\n\n{text}")
        if rel == "wiki/index.md":
            continue
        for next_ref in extract_wiki_refs(text):
            if next_ref not in visited and next_ref not in queue:
                queue.append(next_ref)

    return "".join(chunks), ordered


def read_wiki_page(wiki_home: Path, rel: str) -> str:
    path = wiki_home / rel
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def read_wiki_pages(wiki_home: Path, pages: list[str]) -> str:
    chunks: list[str] = []
    for rel in pages:
        text = read_wiki_page(wiki_home, rel)
        if text:
            chunks.append(f"\n\n# {rel}\n\n{text}")
    return "".join(chunks)


def estimate_tokens(text: str) -> int:
    # Chinese-heavy prompt rough estimate. Used for budgeting and run records only.
    return round(len(text) / 1.5)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def extract_numbered_section(text: str, section_number: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(section_number)}\.\s.*?(?=^##\s+\d+\.|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(0) if match else ""


def extract_required_fields(entry_guide: str, artifact_name: str) -> list[str]:
    pattern = re.compile(
        rf"`{re.escape(artifact_name)}`\s*至少包含：\s*```text\s*(.*?)\s*```",
        flags=re.DOTALL,
    )
    match = pattern.search(entry_guide)
    if not match:
        return []
    fields: list[str] = []
    for line in match.group(1).splitlines():
        field = line.strip()
        if not field:
            continue
        field = re.split(r"[：:]", field, maxsplit=1)[0].strip()
        if field:
            fields.append(field)
    return fields


def extract_section_wiki_refs(entry_guide: str, section_number: str) -> list[str]:
    return extract_wiki_refs(extract_numbered_section(entry_guide, section_number))


def extract_base_route_refs(entry_guide: str) -> list[str]:
    section = extract_numbered_section(entry_guide, "5")
    match = re.search(
        r"###\s+5\.4\s+必须路由的基础知识\s*(.*?)(?=\n如果画像命中|\n###\s+5\.5|\Z)",
        section,
        flags=re.DOTALL,
    )
    return extract_wiki_refs(match.group(1) if match else "")


def extract_conditional_route_refs(entry_guide: str) -> list[tuple[str, list[str]]]:
    section = extract_numbered_section(entry_guide, "5")
    requirements: list[tuple[str, list[str]]] = []
    for match in re.finditer(
        r"如果画像命中(.+?)，必须额外路由：\s*(.*?)(?=\n###|\Z)",
        section,
        flags=re.DOTALL,
    ):
        requirements.append((match.group(1).strip(), extract_wiki_refs(match.group(2))))
    return requirements


def wiki_ref_present(content: str, ref: str) -> bool:
    candidates = {ref}
    if ref.endswith(".md"):
        candidates.add(ref[:-3])
    else:
        candidates.add(f"{ref}.md")
    return any(candidate in content for candidate in candidates)


def validate_wiki_protocol_output(
    stage_name: str,
    content: str,
    required_fields: list[str],
    required_refs: list[str] | None = None,
    required_action_ids: list[str] | None = None,
) -> list[str]:
    issues: list[str] = []
    missing_fields = [field for field in required_fields if field not in content]
    if missing_fields:
        issues.append("缺少 Wiki 协议必填字段：" + "、".join(missing_fields))

    missing_refs = [ref for ref in (required_refs or []) if not wiki_ref_present(content, ref)]
    if missing_refs:
        issues.append("缺少 Wiki 协议要求路由的知识页：" + "、".join(missing_refs))

    missing_actions = [action_id for action_id in (required_action_ids or []) if action_id not in content]
    if missing_actions:
        issues.append("缺少 Wiki 协议要求的动作ID：" + "、".join(missing_actions))

    if issues:
        issues.insert(0, f"{stage_name} 未通过 LLM Wiki 协议结构校验")
    return issues


@dataclass(frozen=True)
class WikiProtocolAction:
    source_page: str
    action_id: str
    action_name: str
    required_sections: str
    trigger_signals: str
    required_checks: str
    review_points: str
    must_record_miss: str


@dataclass(frozen=True)
class ActionTask:
    action_id: str
    action_name: str
    source: str
    required_sections: str
    trigger_signals: str
    required_checks: str
    raw_row: str


@dataclass(frozen=True)
class ActionBatch:
    batch_id: str
    label: str
    tasks: list[ActionTask]
    source_excerpt: str
    reason: str
    input_mode: str
    source_chars: int
    check_count: int


def strip_markdown_cell(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "；", text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_markdown_table_rows(content: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_headers: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [strip_markdown_cell(cell) for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        if "动作ID" in cells:
            current_headers = cells
            continue
        if not current_headers or len(cells) < len(current_headers):
            continue
        row = {header: cells[index] for index, header in enumerate(current_headers)}
        row["__raw__"] = stripped
        rows.append(row)
    return rows


def parse_action_tasks(actions: str) -> list[ActionTask]:
    tasks: list[ActionTask] = []
    seen: set[str] = set()
    for row in parse_markdown_table_rows(actions):
        action_id = row.get("动作ID", "").strip()
        if not action_id or action_id in seen:
            continue
        if action_id not in extract_action_ids_from_content(action_id):
            continue
        seen.add(action_id)
        tasks.append(
            ActionTask(
                action_id=action_id,
                action_name=row.get("动作名称", ""),
                source=row.get("来源知识", ""),
                required_sections=row.get("必读章节", ""),
                trigger_signals=row.get("触发信号", ""),
                required_checks=row.get("必须检查", ""),
                raw_row=row.get("__raw__", ""),
            )
        )
    return tasks


def extract_field_from_block(block: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}::\s*(.+?)\s*$", block, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_wiki_protocol_actions(wiki_home: Path, pages: list[str]) -> list[WikiProtocolAction]:
    actions: list[WikiProtocolAction] = []
    seen: set[tuple[str, str]] = set()

    def add_action(
        normalized: str,
        action_id: str,
        action_name: str,
        required_sections: str = "",
        trigger_signals: str = "",
        required_checks: str = "",
        review_points: str = "",
        must_record_miss: str = "",
    ) -> None:
        action_id = action_id.strip()
        if not action_id or action_id not in extract_action_ids_from_content(action_id):
            return
        key = (normalized, action_id)
        if key in seen:
            return
        seen.add(key)
        actions.append(
            WikiProtocolAction(
                source_page=normalized,
                action_id=action_id,
                action_name=action_name.strip() or action_id,
                required_sections=required_sections.strip(),
                trigger_signals=trigger_signals.strip(),
                required_checks=required_checks.strip(),
                review_points=review_points.strip(),
                must_record_miss=must_record_miss.strip(),
            )
        )

    for rel in pages:
        normalized = normalize_wiki_ref(rel) or rel
        if not is_allowed_knowledge_page(normalized):
            continue
        text = read_wiki_page(wiki_home, normalized)
        if "动作ID::" in text:
            blocks = re.split(r"(?=^###\s+)", text, flags=re.MULTILINE)
            for block in blocks:
                action_id = extract_field_from_block(block, "动作ID")
                if not action_id:
                    continue
                heading_match = re.search(r"^###\s+(.+?)\s*$", block, flags=re.MULTILINE)
                heading = heading_match.group(1).strip() if heading_match else ""
                action_name = extract_field_from_block(block, "动作名称")
                if not action_name and heading.startswith(action_id):
                    action_name = heading[len(action_id) :].strip()
                if not action_name:
                    action_name = heading or action_id
                add_action(
                    normalized=normalized,
                    action_id=action_id,
                    action_name=action_name,
                    required_sections=extract_field_from_block(block, "必读章节"),
                    trigger_signals=extract_field_from_block(block, "触发信号"),
                    required_checks=extract_field_from_block(block, "必须检查"),
                    review_points=extract_field_from_block(block, "关联审查点"),
                    must_record_miss=extract_field_from_block(block, "未命中也必须记录"),
                )
        if normalized in CORE_EXECUTION_PAGES:
            continue
        for row in parse_markdown_table_rows(text):
            action_id = row.get("动作ID", "").strip()
            if not action_id:
                continue
            lookup = row.get("查找对象", "") or row.get("检查对象", "") or row.get("触发信号", "")
            checks = row.get("必须检查", "") or lookup
            expected = row.get("预期输出", "") or row.get("输出", "") or row.get("输出要求", "")
            add_action(
                normalized=normalized,
                action_id=action_id,
                action_name=row.get("动作名称", ""),
                required_sections=row.get("必读章节", ""),
                trigger_signals=lookup,
                required_checks=checks,
                review_points=row.get("关联风险审查点", ""),
                must_record_miss=row.get("未命中也必须记录", "") or row.get("是否必做", "") or expected,
            )
    return actions


def format_wiki_protocol_action_baseline(actions: list[WikiProtocolAction]) -> str:
    if not actions:
        return "- 未从已路由 Wiki 页解析到结构化动作。"
    rows = [
        "| 协议页 | 动作ID | Wiki动作名称 | 必读章节 | 触发信号 | 必须检查 | 关联审查点 | 未命中也必须记录 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for action in actions:
        rows.append(
            "| {source} | {action_id} | {name} | {sections} | {signals} | {checks} | {points} | {record} |".format(
                source=action.source_page,
                action_id=action.action_id,
                name=action.action_name,
                sections=action.required_sections,
                signals=action.trigger_signals,
                checks=action.required_checks,
                points=action.review_points,
                record=action.must_record_miss,
            )
        )
    return "\n".join(rows)


def line_contains_action_name(content: str, action: WikiProtocolAction) -> bool:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if action.action_id not in line:
            continue
        window = "\n".join(lines[index : index + 4])
        if action.action_name and action.action_name in window:
            return True
    return False


def validate_wiki_action_list(content: str, actions: list[WikiProtocolAction]) -> list[str]:
    if not actions:
        return []
    issues: list[str] = []
    missing = [action.action_id for action in actions if action.action_id not in content]
    if missing:
        issues.append("动作清单缺少已路由 Wiki 协议动作ID：" + "、".join(missing))
    name_mismatch = [
        f"{action.action_id}（Wiki动作名称：{action.action_name}）"
        for action in actions
        if action.action_id in content and action.action_name and not line_contains_action_name(content, action)
    ]
    if name_mismatch:
        issues.append("动作清单存在动作ID语义错位或未保留 Wiki 动作名称：" + "、".join(name_mismatch))
    return issues


def extract_action_ids_from_content(content: str) -> list[str]:
    ids: list[str] = []
    patterns = (
        r"(?<![A-Za-z0-9])([A-Z][A-Za-z]*-[A-Z]?\d{2,3}[A-Z]?)(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9一-龥])([一-龥]{1,12}-[A-Za-z]?\d{2,3}[A-Z]?)(?![A-Za-z0-9])",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            action_id = match.group(1).strip()
            if action_id not in ids:
                ids.append(action_id)
    return ids


def validate_action_execution_coverage(content: str, action_ids: list[str]) -> list[str]:
    missing = [action_id for action_id in action_ids if action_id not in content]
    if missing:
        return ["动作执行记录缺少动作清单中的动作ID：" + "、".join(missing)]
    return []


def validate_no_unknown_action_ids(content: str, allowed_action_ids: list[str], stage_name: str) -> list[str]:
    allowed = set(allowed_action_ids)
    non_action_prefixes = {"R", "AR", "QG", "QM", "RISK", "待"}
    non_action_suffix_initials = {"R"}
    sanitized = content
    for action_id in sorted(allowed, key=len, reverse=True):
        sanitized = re.sub(rf"{re.escape(action_id)}-C\d{{2,3}}", "", sanitized)
        sanitized = sanitized.replace(action_id, "")
    unknown = [
        action_id
        for action_id in extract_action_ids_from_content(sanitized)
        if action_id not in allowed
        and action_id.split("-", 1)[0] not in non_action_prefixes
        and action_id.split("-", 1)[1][:1] not in non_action_suffix_initials
    ]
    if unknown:
        return [f"{stage_name} 出现动作清单之外的动作ID：" + "、".join(unknown)]
    return []


def validate_action_protocol_refs_are_routed(content: str, routed_refs: list[str]) -> list[str]:
    allowed_refs = {normalize_wiki_ref(ref) or ref for ref in [*CORE_EXECUTION_PAGES, *routed_refs]}
    protocol_refs = [
        ref
        for ref in extract_wiki_refs(content)
        if ref.startswith("wiki/70-审查协议/") and ref not in allowed_refs
    ]
    protocol_refs = list(dict.fromkeys(protocol_refs))
    if protocol_refs:
        return ["动作清单引用了未在知识路由表启用的审查协议页：" + "、".join(protocol_refs)]
    return []


def split_action_field_terms(text: str) -> list[str]:
    terms: list[str] = []
    cleaned = strip_markdown_cell(text)
    for part in re.split(r"[;；,，、/／\s]+", cleaned):
        part = part.strip(" ：:。()（）[]【】")
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    if cleaned and cleaned not in terms:
        terms.append(cleaned)
    return terms


def primary_action_section(task: ActionTask) -> str:
    for term in split_action_field_terms(task.required_sections):
        return term
    return "未标明必读章节"


def build_global_action_context(numbered_text: str, max_chars: int = 14000) -> str:
    lines = numbered_text.splitlines()
    if not lines:
        return ""
    head = "\n".join(lines[:180])
    if len(head) >= max_chars:
        return head[:max_chars]
    return head


def build_line_windows(
    numbered_text: str,
    terms: list[str],
    radius: int = 34,
    max_matches_per_term: int = 10,
    max_chars: int = 76000,
) -> str:
    lines = numbered_text.splitlines()
    ranges: list[tuple[int, int]] = []
    used_terms = sorted([term for term in terms if len(term) >= 2], key=lambda item: (-len(item), item))
    for term in used_terms:
        matches = [index for index, line in enumerate(lines) if term in line]
        if len(matches) > max_matches_per_term * 6 and len(term) <= 3:
            continue
        if len(matches) > max_matches_per_term * 12:
            continue
        for index in matches[:max_matches_per_term]:
            ranges.append((max(0, index - radius), min(len(lines), index + radius + 1)))
    if not ranges:
        return ""
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    excerpts: list[str] = []
    used_chars = 0
    for start, end in merged:
        excerpt = "\n".join(lines[start:end])
        if excerpts and used_chars + len(excerpt) > max_chars:
            break
        if not excerpts and len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars]
        excerpts.append(excerpt)
        used_chars += len(excerpt)
    return "\n\n...\n\n".join(excerpts)


def action_batch_label(tasks: list[ActionTask]) -> str:
    labels: list[str] = []
    for task in tasks:
        label = primary_action_section(task)
        if label not in labels:
            labels.append(label)
    return "、".join(labels[:4]) or "综合动作"


def collect_action_terms(tasks: list[ActionTask]) -> list[str]:
    terms: list[str] = []
    for task in tasks:
        for source in (task.action_name, task.required_sections, task.trigger_signals, task.required_checks):
            for term in split_action_field_terms(source):
                if term not in terms:
                    terms.append(term)
    return terms


def build_action_source_excerpt(numbered_text: str, tasks: list[ActionTask]) -> tuple[str, str]:
    if len(numbered_text) <= 90000:
        return numbered_text, "完整抽取文本"
    global_context = build_global_action_context(numbered_text)
    window_context = build_line_windows(numbered_text, collect_action_terms(tasks))
    parts = []
    if global_context:
        parts.append("## 全局前置信息窗口\n\n" + global_context)
    if window_context:
        parts.append("## 本批动作相关原文窗口\n\n" + window_context)
    if not parts:
        parts.append("\n".join(numbered_text.splitlines()[:260]))
    return "\n\n...\n\n".join(parts), "全局前置信息窗口 + 本批动作相关原文窗口"


def action_complexity_reason(task: ActionTask) -> str:
    scope = " ".join([task.action_name, task.required_sections, task.trigger_signals, task.required_checks])
    key_scoring_terms = ("分值闭合", "总分", "合计", "评分分值", "权重闭合")
    if any(term in scope for term in key_scoring_terms):
        return "评分分值闭合类动作单独执行，保留算术核验上下文"
    return ""


def action_check_count(task: ActionTask) -> int:
    return len(split_required_checks(task.required_checks))


def build_action_batches(actions: str, numbered_text: str) -> list[ActionBatch]:
    tasks = parse_action_tasks(actions)
    batches: list[ActionBatch] = []
    current: list[ActionTask] = []
    current_checks = 0

    def append_batch(group_tasks: list[ActionTask], reason: str) -> None:
        label = action_batch_label(group_tasks)
        excerpt, input_mode = build_action_source_excerpt(numbered_text, group_tasks)
        check_count = sum(action_check_count(task) for task in group_tasks)
        batches.append(
            ActionBatch(
                batch_id=f"动作组{len(batches) + 1:02d}",
                label=label,
                tasks=group_tasks,
                source_excerpt=excerpt,
                reason=reason,
                input_mode=input_mode,
                source_chars=len(excerpt),
                check_count=check_count,
            )
        )

    for task in tasks:
        complex_reason = action_complexity_reason(task)
        task_checks = action_check_count(task)
        if complex_reason:
            if current:
                append_batch(current, "相邻普通动作合并执行，控制批次大小并保留上下文")
                current = []
                current_checks = 0
            append_batch([task], complex_reason)
            continue
        if len(current) >= 4 or current_checks + task_checks > 14:
            append_batch(current, "相邻普通动作合并执行，控制批次大小并保留上下文")
            current = []
            current_checks = 0
        current.append(task)
        current_checks += task_checks
    if current:
        append_batch(current, "相邻普通动作合并执行，控制批次大小并保留上下文")
    return batches


def format_action_batch_tasks(tasks: list[ActionTask]) -> str:
    rows = [
        "| 动作ID | 动作名称 | 来源知识 | 必读章节 | 触发信号 | 必须检查 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for task in tasks:
        rows.append(
            f"| {task.action_id} | {task.action_name} | {task.source} | {task.required_sections} | {task.trigger_signals} | {task.required_checks} |"
        )
    return "\n".join(rows)


def split_required_checks(text: str) -> list[str]:
    cleaned = strip_markdown_cell(text)
    if not cleaned:
        return []
    parts = [
        part.strip(" ：:。；;")
        for part in re.split(r"(?:^|[;；]\s*)\d+[.、]\s*", cleaned)
        if part.strip(" ：:。；;")
    ]
    if len(parts) <= 1:
        parts = [
            part.strip(" ：:。；;")
            for part in re.split(r"[;；]\s*", cleaned)
            if part.strip(" ：:。；;")
        ]
    return parts or [cleaned]


def action_required_check_items(tasks: list[ActionTask]) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    for task in tasks:
        checks = split_required_checks(task.required_checks)
        if not checks:
            continue
        for index, check in enumerate(checks, start=1):
            check_id = f"{task.action_id}-C{index:02d}"
            items.append((task.action_id, check_id, check))
    return items


def format_required_checklist(tasks: list[ActionTask]) -> str:
    rows = [
        "| 动作ID | 检查项ID | 必须检查项 |",
        "| --- | --- | --- |",
    ]
    for action_id, check_id, check in action_required_check_items(tasks):
        rows.append(f"| {action_id} | {check_id} | {check} |")
    return "\n".join(rows)


def validate_required_check_coverage(content: str, tasks: list[ActionTask]) -> list[str]:
    missing = [check_id for _, check_id, _ in action_required_check_items(tasks) if check_id not in content]
    if not missing:
        return []
    return ["04-动作执行记录缺少必须检查项执行结果：" + "、".join(missing)]


def extract_wiki_page_for_action_ids(text: str, action_ids: list[str]) -> str:
    if "动作ID::" not in text:
        return text
    allowed = set(action_ids)
    blocks = re.split(r"(?=^###\s+)", text, flags=re.MULTILINE)
    kept: list[str] = []
    preface = blocks[0].strip() if blocks else ""
    if preface and "动作ID::" not in preface:
        kept.append(preface)
    for block in blocks:
        block_action_id = extract_field_from_block(block, "动作ID")
        if block_action_id and block_action_id in allowed:
            kept.append(block.strip())
    return "\n\n".join(part for part in kept if part)


def budget_wiki_pages_for_actions(
    wiki_home: Path,
    pages: list[str],
    action_ids: list[str],
    char_budget: int,
) -> tuple[str, list[str]]:
    chunks: list[str] = []
    loaded: list[str] = []
    seen: set[str] = set()
    used = 0
    for rel in pages:
        rel = normalize_wiki_ref(rel) or rel
        if rel in seen or not is_allowed_knowledge_page(rel):
            continue
        seen.add(rel)
        text = read_wiki_page(wiki_home, rel)
        if not text:
            continue
        scoped_text = extract_wiki_page_for_action_ids(text, action_ids)
        if not scoped_text.strip():
            continue
        chunk = f"\n\n# {rel}\n\n{scoped_text}"
        if chunks and used + len(chunk) > char_budget:
            continue
        chunks.append(chunk)
        loaded.append(rel)
        used += len(chunk)
    return "".join(chunks), loaded


def build_wiki_action_coverage_rows(actions: list[WikiProtocolAction], action_list: str, action_exec: str) -> str:
    if not actions:
        return "| 未解析到结构化 Wiki 协议动作 | - | - | - | - |\n"
    rows: list[str] = []
    for action in actions:
        in_action_list = action.action_id in action_list
        in_action_exec = action.action_id in action_exec
        semantic_ok = line_contains_action_name(action_list, action)
        rows.append(
            f"| {action.source_page} | {action.action_id} | {action.action_name} | {in_action_list} | {in_action_exec} | {semantic_ok} |"
        )
    return "\n".join(rows)


def deterministic_action_coverage_summary(action_ids: list[str], action_list: str, action_exec: str) -> tuple[str, list[str]]:
    rows = [
        "| 动作ID | 进入动作清单 | 进入动作执行记录 |",
        "| --- | --- | --- |",
    ]
    issues: list[str] = []
    for action_id in action_ids:
        in_action_list = action_id in action_list
        in_action_exec = action_id in action_exec
        rows.append(f"| {action_id} | {in_action_list} | {in_action_exec} |")
        if not in_action_list:
            issues.append(f"{action_id} 未进入动作清单")
        if not in_action_exec:
            issues.append(f"{action_id} 未进入动作执行记录")
    conclusion = "通过" if not issues else "不通过：" + "；".join(issues)
    return "\n".join(rows) + f"\n\n确定性动作覆盖结论：{conclusion}", issues


def validate_quality_not_contradict_action_coverage(
    content: str,
    covered_action_ids: list[str],
    coverage_issues: list[str],
) -> list[str]:
    if coverage_issues:
        return []
    issues: list[str] = []
    coverage_conflict_patterns = (
        r"动作(?:清单|执行记录)?[^。\n|]{0,30}(?:缺失|未包含|未列出|未进入|缺乏|未覆盖|未执行|没有)",
        r"(?:缺失|未包含|未列出|未进入|缺乏|未覆盖|未执行|没有)[^。\n|]{0,30}动作(?:清单|执行记录)?",
        r"未进入\s*`?03",
        r"未进入\s*`?04",
        r"缺少[^。\n|]{0,30}动作ID",
    )
    negation_patterns = ("无缺失", "未发现缺失", "不存在缺失", "没有缺失", "无未执行", "均已", "全部", "完整覆盖")
    for action_id in covered_action_ids:
        for match in re.finditer(re.escape(action_id), content):
            window = content[max(0, match.start() - 80) : match.end() + 80]
            if any(pattern in window for pattern in negation_patterns):
                continue
            if any(re.search(pattern, window) for pattern in coverage_conflict_patterns):
                issues.append(f"质量门结论与确定性动作覆盖校验冲突：{action_id} 已覆盖，但输出称其缺失或未执行")
                break
    return issues


def combine_validators(*validators: Callable[[str], list[str]]) -> Callable[[str], list[str]]:
    def combined(content: str) -> list[str]:
        issues: list[str] = []
        for validator in validators:
            issues.extend(validator(content))
        return issues

    return combined


def parse_decimal(value: str) -> float:
    return float(value.replace("，", "").replace(",", ""))


def format_decimal(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def arithmetic_consistency_issues(content: str) -> list[str]:
    """Check only arithmetic self-consistency in model output, not review legality."""
    issues: list[str] = []
    seen: set[str] = set()

    equation_pattern = re.compile(
        r"(\d+(?:[.,]\d+)?(?:\s*[+＋]\s*\d+(?:[.,]\d+)?){2,})\s*[=＝]\s*(\d+(?:[.,]\d+)?)"
    )
    for match in equation_pattern.finditer(content):
        terms = [parse_decimal(term) for term in re.findall(r"\d+(?:[.,]\d+)?", match.group(1))]
        claimed = parse_decimal(match.group(2))
        actual = sum(terms)
        if abs(actual - claimed) > 0.01:
            key = f"equation:{match.group(0)}"
            if key not in seen:
                seen.add(key)
                issues.append(
                    "算术表达不自洽："
                    f"`{match.group(0)}` 实际合计为 {format_decimal(actual)}，"
                    f"不是 {format_decimal(claimed)}"
                )

    return issues


def usage_number(usage: dict, key: str) -> int:
    value = usage.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def output_hit_limit(usage: dict, max_tokens: int) -> bool:
    completion_tokens = usage_number(usage, "completion_tokens")
    return completion_tokens >= max(1, int(max_tokens * 0.98))


def context_limit_retry_tokens(error: Exception, requested_max_tokens: int) -> int | None:
    message = str(error)
    if "input tokens" not in message or ("maximum input length" not in message and "context length" not in message):
        return None
    input_match = re.search(r"passed\s+(\d+)\s+input tokens", message)
    context_match = re.search(r"context length is only\s+(\d+)", message)
    limit_match = re.search(r"maximum input length is only\s+(\d+)", message)
    if not input_match:
        return max(128, requested_max_tokens - 1000)
    input_tokens = int(input_match.group(1))
    if context_match:
        context_tokens = int(context_match.group(1))
    elif limit_match:
        context_tokens = int(limit_match.group(1)) + requested_max_tokens
    else:
        return max(128, requested_max_tokens - 1000)
    next_max_tokens = context_tokens - input_tokens - 256
    if next_max_tokens >= requested_max_tokens or next_max_tokens < 128:
        return None
    return next_max_tokens


def budget_wiki_pages(wiki_home: Path, pages: list[str], char_budget: int) -> tuple[str, list[str]]:
    chunks: list[str] = []
    loaded: list[str] = []
    seen: set[str] = set()
    used = 0
    for rel in pages:
        rel = normalize_wiki_ref(rel) or rel
        if rel in seen or not is_allowed_knowledge_page(rel):
            continue
        seen.add(rel)
        text = read_wiki_page(wiki_home, rel)
        if not text:
            continue
        chunk = f"\n\n# {rel}\n\n{text}"
        if chunks and used + len(chunk) > char_budget:
            continue
        chunks.append(chunk)
        loaded.append(rel)
        used += len(chunk)
    return "".join(chunks), loaded


def filter_wiki_pages_by_allowed_actions(wiki_home: Path, pages: list[str], allowed_action_ids: list[str]) -> list[str]:
    allowed = set(allowed_action_ids)
    core_pages = {normalize_wiki_ref(ref) or ref for ref in CORE_EXECUTION_PAGES}
    filtered: list[str] = []
    for rel in pages:
        normalized = normalize_wiki_ref(rel) or rel
        if normalized in filtered:
            continue
        if normalized in core_pages:
            filtered.append(normalized)
            continue
        text = read_wiki_page(wiki_home, normalized)
        page_action_ids = extract_action_ids_from_content(text)
        if page_action_ids and any(action_id not in allowed for action_id in page_action_ids):
            continue
        filtered.append(normalized)
    return filtered


CORE_EXECUTION_PAGES = [
    ENTRY_GUIDE,
    "wiki/70-审查协议/知识驱动审查执行规范.md",
    "wiki/70-审查协议/政府采购招标文件业务审查流水线.md",
    "wiki/70-审查协议/政府采购招标文件审查协议.md",
    "wiki/20-知识点/知识分层与路由规则.md",
    "wiki/20-知识点/政府采购逐章审查矩阵.md",
    "wiki/70-审查协议/风险原子化规则.md",
    "wiki/70-审查协议/质量门规则.md",
    "wiki/25-风险审查点/风险审查点总览.md",
    "wiki/90-模板/审查记录模板.md",
    "wiki/90-模板/AI调度运行记录模板.md",
]


PROFILE_PAGES = [
    ENTRY_GUIDE,
    "wiki/20-知识点/政府采购招标文件画像.md",
    "wiki/15-行业基础/政府采购专项场景画像.md",
    "wiki/60-提示词/招标文件画像提示词.md",
]


def risk_review_point_catalog(wiki_home: Path) -> str:
    root = wiki_home / "wiki/25-风险审查点"
    if not root.is_dir():
        return ""
    rows: list[str] = []
    for path in sorted(root.glob("*.md")):
        rel = path.relative_to(wiki_home).as_posix()
        title = path.stem
        rows.append(f"- [[{rel}]] {title}")
    return "\n".join(rows)


def law_catalog(wiki_home: Path) -> str:
    root = wiki_home / "wiki/10-法规依据"
    if not root.is_dir():
        return ""
    rows: list[str] = []
    for path in sorted(root.glob("*.md")):
        rel = path.relative_to(wiki_home).as_posix()
        title = path.stem
        rows.append(f"- [[{rel}]] {title}")
    return "\n".join(rows)


def stage_file(path: Path, title: str, content: str) -> None:
    path.write_text(f"# {title}\n\n{content.rstrip()}\n", encoding="utf-8")


PROMPT_OUTPUT_DIR = "outputs/<CATEGORY>/<RUN_ID>"
PROMPT_EXTRACT_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-抽取文本.txt"
PROMPT_PROFILE_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-01-文件画像.md"
PROMPT_ROUTE_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-02-知识路由表.md"
PROMPT_ACTIONS_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-03-动作清单.md"
PROMPT_ACTION_EXEC_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-04-动作执行记录.md"
PROMPT_ATOMIZED_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-05-原子风险清单.md"
PROMPT_QUALITY_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-06-质量门检查表.md"
PROMPT_RUN_REL = f"{PROMPT_OUTPUT_DIR}/<PROJECT>-AI调度运行记录.md"
PROMPT_REVIEW_START = "<REVIEW_START_TIME>"
PROMPT_REVIEW_END = "<REVIEW_END_TIME>"


def replace_prompt_placeholders(text: str, replacements: dict[str, str]) -> str:
    for placeholder, value in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(placeholder, value)
    return text


def remove_legacy_report_metadata(text: str) -> str:
    legacy_keys = {
        "类型",
        "状态",
        "审查日期",
        "审查时间",
        "审查人",
        "外部标注使用",
        "LLM Wiki修改",
        "LLM Wiki维护命令",
    }
    lines = [
        line
        for line in text.splitlines()
        if not any(line.startswith(f"{key}::") for key in legacy_keys)
    ]
    return "\n".join(lines).strip()


def normalize_report_time_header(text: str, review_start_time: str, review_end_time: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.startswith("审查开始时间::") and not line.startswith("审查结束时间::")
    ]
    body = "\n".join(lines).strip()
    return f"审查开始时间:: {review_start_time}\n审查结束时间:: {review_end_time}\n\n{body}".rstrip()


def chat_text(base_url: str, api_key: str, model: str, prompt: str, max_tokens: int = 8000) -> tuple[str, dict]:
    chat = post_chat_completion(base_url, api_key, model, prompt, max_tokens=max_tokens)
    message = (chat.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "").strip(), chat.get("usage") or {}


def chat_stage(
    base_url: str,
    api_key: str,
    model: str,
    stage_name: str,
    prompt: str,
    max_tokens: int,
    attempt_rows: list[dict[str, str | int | bool]],
    max_retries: int = 1,
    validator: Callable[[str], list[str]] | None = None,
) -> tuple[str, dict]:
    current_prompt = prompt
    current_max_tokens = max_tokens
    last_issues: list[str] = []
    for attempt in range(1, max_retries + 2):
        while True:
            try:
                content, usage = chat_text(base_url, api_key, model, current_prompt, max_tokens=current_max_tokens)
                break
            except RuntimeError as exc:
                next_max_tokens = context_limit_retry_tokens(exc, current_max_tokens)
                if next_max_tokens is None:
                    raise
                current_max_tokens = next_max_tokens
        hit_limit = output_hit_limit(usage, current_max_tokens)
        validation_issues = validator(content) if content and validator else []
        last_issues = validation_issues
        attempt_rows.append(
            {
                "stage": stage_name,
                "attempt": attempt,
                "max_tokens": current_max_tokens,
                "prompt_hash": text_hash(current_prompt),
                "output_hash": text_hash(content),
                "prompt_tokens": usage_number(usage, "prompt_tokens"),
                "completion_tokens": usage_number(usage, "completion_tokens"),
                "total_tokens": usage_number(usage, "total_tokens"),
                "hit_limit": hit_limit,
                "protocol_ok": bool(content and not validation_issues),
                "issues": "；".join(validation_issues),
            }
        )
        if content and not hit_limit and not validation_issues:
            return content, usage
        if attempt > max_retries:
            if not content:
                raise RuntimeError(f"{stage_name} returned empty content")
            if validation_issues:
                raise RuntimeError(f"{stage_name} failed Wiki protocol check: {'; '.join(validation_issues)}")
            raise RuntimeError(f"{stage_name} output reached max_tokens limit; stage result is not trusted")
        current_max_tokens = min(current_max_tokens * 2, 24000)
        issue_text = "\n".join(f"- {issue}" for issue in validation_issues)
        retry_reason = issue_text or f"- `{stage_name}` 输出疑似为空或触达 max_tokens 上限。"
        current_prompt = f"""{prompt}

## 重试要求

上一轮未通过，原因：
{retry_reason}

本轮必须严格按 LLM Wiki 协议输出完整中间产物。
不得省略必填字段；如内容较多，应优先保留结构化字段、动作状态、原文证据和质量门结论。
如果原因包含“缺少必须检查项执行结果”，必须在逐项检查结果中逐字补齐列出的检查项ID；每个检查项ID都必须单独出现，并给出执行状态、读取范围、原文位置或未命中原因、判断结果。
"""
    raise RuntimeError(f"{stage_name} failed: {'; '.join(last_issues)}")


def post_chat_completion(base_url: str, api_key: str, model: str, prompt: str, max_tokens: int = 16000) -> dict:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是政府采购招标文件合规审查生产线。只输出审查报告 Markdown，不要输出解释性前言。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"chat completion failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"chat completion connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("chat completion timed out") from exc


def direct_chat_review(
    target: str,
    target_path: Path,
    biz_home: Path,
    wiki_home: Path,
    config_dir: Path,
    resume_output: Path | None = None,
) -> int:
    config_toml = config_dir / "config.toml"
    base_url = read_toml_string(config_toml, "base_url")
    model = read_toml_string(config_toml, "model")
    api_key = read_auth_key(config_dir)
    if not base_url or not model or not api_key:
        print("direct chat config incomplete", file=sys.stderr)
        return 1

    now = local_now()
    review_date = now.strftime("%Y-%m-%d")
    review_time = now.strftime("%H:%M:%S CST")
    review_start_time = now.strftime("%Y-%m-%d %H:%M:%S CST")
    category = output_category(target)
    if resume_output is not None:
        output_dir = resume_output if resume_output.is_absolute() else biz_home / resume_output
        output_dir = output_dir.resolve()
        try:
            output_rel_dir = output_dir.relative_to(biz_home.resolve()).as_posix()
        except ValueError as exc:
            raise RuntimeError("resume output must be inside the business project") from exc
        if not output_dir.is_dir():
            raise RuntimeError("resume output directory not found")
    else:
        output_dir, output_rel_dir = make_run_output_dir(biz_home, category, now)

    stem = target_path.stem
    extract_rel = f"{output_rel_dir}/{stem}-抽取文本.txt"
    profile_rel = f"{output_rel_dir}/{stem}-01-文件画像.md"
    route_rel = f"{output_rel_dir}/{stem}-02-知识路由表.md"
    actions_rel = f"{output_rel_dir}/{stem}-03-动作清单.md"
    action_exec_rel = f"{output_rel_dir}/{stem}-04-动作执行记录.md"
    atomized_rel = f"{output_rel_dir}/{stem}-05-原子风险清单.md"
    quality_rel = f"{output_rel_dir}/{stem}-06-质量门检查表.md"
    report_rel = f"{output_rel_dir}/{stem}-审查报告.md"
    run_rel = f"{output_rel_dir}/{stem}-AI调度运行记录.md"
    extract_path = biz_home / extract_rel
    profile_path = biz_home / profile_rel
    route_path = biz_home / route_rel
    actions_path = biz_home / actions_rel
    action_exec_path = biz_home / action_exec_rel
    atomized_path = biz_home / atomized_rel
    quality_path = biz_home / quality_rel
    report_path = biz_home / report_rel
    run_path = biz_home / run_rel
    report_replacements = {
        PROMPT_OUTPUT_DIR: output_rel_dir,
        PROMPT_EXTRACT_REL: extract_rel,
        PROMPT_PROFILE_REL: profile_rel,
        PROMPT_ROUTE_REL: route_rel,
        PROMPT_ACTIONS_REL: actions_rel,
        PROMPT_ACTION_EXEC_REL: action_exec_rel,
        PROMPT_ATOMIZED_REL: atomized_rel,
        PROMPT_QUALITY_REL: quality_rel,
        PROMPT_RUN_REL: run_rel,
        PROMPT_REVIEW_START: review_start_time,
    }

    if extract_path.is_file():
        numbered_text = extract_path.read_text(encoding="utf-8")
    else:
        raw_text = extract_file_text(target_path)
        numbered_text = line_number_text(raw_text)
        extract_path.write_text(numbered_text + "\n", encoding="utf-8")

    usages: list[tuple[str, dict]] = []
    prompt_stats: list[tuple[str, int, int]] = []
    attempt_rows: list[dict[str, str | int | bool]] = []
    stage_paths = [
        ("01-文件画像", profile_rel),
        ("02-知识路由表", route_rel),
        ("03-动作清单", actions_rel),
        ("04-动作执行记录", action_exec_rel),
        ("05-原子风险清单", atomized_rel),
        ("06-质量门检查表", quality_rel),
        ("07-AI审查记录", report_rel),
        ("08-运行记录", run_rel),
    ]

    entry_guide_text = read_wiki_page(wiki_home, ENTRY_GUIDE)
    protocol_fields = {
        "01-文件画像": extract_required_fields(entry_guide_text, "01-文件画像"),
        "02-知识路由表": extract_required_fields(entry_guide_text, "02-知识路由表"),
        "03-动作清单": extract_required_fields(entry_guide_text, "03-动作清单"),
        "04-动作执行记录": extract_required_fields(entry_guide_text, "04-动作执行记录"),
        "05-原子风险清单": extract_required_fields(entry_guide_text, "05-原子风险清单"),
        "06-质量门检查表": extract_required_fields(entry_guide_text, "06-质量门检查表"),
    }
    conditional_route_refs = extract_conditional_route_refs(entry_guide_text)
    base_route_refs = extract_base_route_refs(entry_guide_text)

    core_knowledge, core_pages = budget_wiki_pages(wiki_home, CORE_EXECUTION_PAGES, char_budget=52000)
    profile_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["01-文件画像"])
    route_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["02-知识路由表"])
    actions_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["03-动作清单"])
    action_exec_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["04-动作执行记录"])
    atomized_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["05-原子风险清单"])
    quality_required_field_lines = "\n".join(f"- {field}" for field in protocol_fields["06-质量门检查表"])

    shared_context = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

本次执行必须由 LLM Wiki 入口指引驱动：先读取入口指引，再按入口指引、流水线、执行规范、质量门和模板要求执行。
执行器只负责调度和落文件，不提供任何审查知识；风险判断只能来自 LLM Wiki 知识和待审文件原文。

边界：
- 不得使用外部标注、标准答案、人工批注或同一项目历史审查记录。
- 不得修改 LLM Wiki。
- 不得对 LLM Wiki 运行 ./lint、./ingest、./query 或其他维护命令。
- 报告和中间产物不得出现绝对路径。
- 原文内容字段只能放待审文件原文。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 输出目录：{PROMPT_OUTPUT_DIR}

本阶段核心 LLM Wiki 知识：
{core_knowledge}
"""

    profile_knowledge, profile_pages = budget_wiki_pages(wiki_home, PROFILE_PAGES, char_budget=26000)

    profile_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请先按 LLM Wiki 入口指引执行文件画像。本阶段只做画像，不识别风险，不生成报告。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 输出目录：{PROMPT_OUTPUT_DIR}

画像阶段 LLM Wiki 知识：
{profile_knowledge}

本阶段 `01-文件画像` 必须包含以下 Wiki 协议字段：
{profile_required_field_lines}

待审文件，已加行号：
{numbered_text}

请执行入口指引中的环节一：文件画像。

只输出 `01-文件画像` Markdown 内容。不得输出风险清单，不得生成最终报告。
必须满足入口指引中 `01-文件画像` 的字段和通过条件；未见字段写 `未见`，不确定字段写 `待确认`。
"""
    if profile_path.is_file():
        profile = profile_path.read_text(encoding="utf-8")
        attempt_rows.append(
            {
                "stage": "01-文件画像",
                "attempt": 0,
                "max_tokens": 0,
                "prompt_hash": "resume",
                "output_hash": text_hash(profile),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "hit_limit": False,
                "protocol_ok": True,
                "issues": "",
            }
        )
    else:
        prompt_stats.append(("01-文件画像", len(profile_prompt), estimate_tokens(profile_prompt)))
        profile, usage = chat_stage(
            base_url,
            api_key,
            model,
            "01-文件画像",
            profile_prompt,
            max_tokens=6000,
            attempt_rows=attempt_rows,
            validator=lambda content: validate_wiki_protocol_output(
                "01-文件画像",
                content,
                protocol_fields["01-文件画像"],
            ),
        )
        usages.append(("01-文件画像", usage))
        stage_file(profile_path, "01-文件画像", profile)

    route_knowledge, route_pages = budget_wiki_pages(
        wiki_home,
        [
            *CORE_EXECUTION_PAGES,
            "wiki/20-知识点/政府采购招标文件画像.md",
            "wiki/15-行业基础/政府采购专项场景画像.md",
        ],
        char_budget=62000,
    )
    route_catalog = f"""## 风险审查点目录

{risk_review_point_catalog(wiki_home)}

## 法规依据目录

{law_catalog(wiki_home)}
"""
    active_route_refs = list(dict.fromkeys(base_route_refs))
    active_route_ref_lines = "\n".join(f"- {ref}" for ref in active_route_refs)

    route_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节二：知识路由。本阶段不读取同一项目历史记录，不输出风险清单，不生成最终报告。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 输出目录：{PROMPT_OUTPUT_DIR}

路由阶段 LLM Wiki 知识：
{route_knowledge}

可选知识目录：
{route_catalog}

本阶段按入口指引必须路由的通用必读知识页，必须全部写入 `02-知识路由表`：
{active_route_ref_lines}

本阶段 `02-知识路由表` 必须包含以下 Wiki 协议字段，字段名不得省略：
{route_required_field_lines}

已生成文件画像：
{profile}

只输出 `02-知识路由表` Markdown 内容。不得输出风险清单，不得生成最终报告。
必须说明每个调用知识页的调用原因、适用层级、是否必读和执行状态。
入口指引中的条件路由规则必须由本阶段基于画像字段和原文证据判断是否启用；执行器不会替你按关键词补充条件路由页。
品类、行业、地域、专题协议页只有在文件画像字段或待审文件原文证据明确支撑时，才能标记为已启用；证据不足时只能标记为候选待确认，不得进入后续动作清单。
调用原因必须写明对应的画像字段或原文证据，不能仅凭单个泛化词、文件夹名称或执行器推断启用品类协议。
每个知识页请尽量使用相对于 LLM Wiki 的稳定路径。
"""
    if route_path.is_file():
        route = route_path.read_text(encoding="utf-8")
        attempt_rows.append(
            {
                "stage": "02-知识路由表",
                "attempt": 0,
                "max_tokens": 0,
                "prompt_hash": "resume",
                "output_hash": text_hash(route),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "hit_limit": False,
                "protocol_ok": True,
                "issues": "",
            }
        )
    else:
        prompt_stats.append(("02-知识路由表", len(route_prompt), estimate_tokens(route_prompt)))
        route, usage = chat_stage(
            base_url,
            api_key,
            model,
            "02-知识路由表",
            route_prompt,
            max_tokens=8000,
            attempt_rows=attempt_rows,
            validator=lambda content: validate_wiki_protocol_output(
                "02-知识路由表",
                content,
                protocol_fields["02-知识路由表"],
                required_refs=active_route_refs,
            ),
        )
        usages.append(("02-知识路由表", usage))
        stage_file(route_path, "02-知识路由表", route)

    routed_refs = extract_enabled_wiki_refs_from_route(route)
    wiki_protocol_actions = extract_wiki_protocol_actions(
        wiki_home,
        [*CORE_EXECUTION_PAGES, *routed_refs],
    )
    wiki_protocol_action_ids = [action.action_id for action in wiki_protocol_actions]
    wiki_protocol_action_baseline = format_wiki_protocol_action_baseline(wiki_protocol_actions)
    action_stage_pages = filter_wiki_pages_by_allowed_actions(
        wiki_home,
        [*CORE_EXECUTION_PAGES, *routed_refs],
        wiki_protocol_action_ids,
    )
    action_knowledge, action_pages = budget_wiki_pages(
        wiki_home,
        action_stage_pages,
        char_budget=62000,
    )
    allowed_protocol_refs = [
        ref
        for ref in [*CORE_EXECUTION_PAGES, *routed_refs]
        if (normalize_wiki_ref(ref) or ref).startswith("wiki/70-审查协议/")
    ]
    allowed_protocol_ref_lines = "\n".join(
        f"- {normalize_wiki_ref(ref) or ref}" for ref in allowed_protocol_refs
    ) or "- 无"

    actions_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节三：动作清单。本阶段只生成动作，不输出风险详情，不生成最终报告。
报告和中间产物不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 输出目录：{PROMPT_OUTPUT_DIR}

动作清单阶段 LLM Wiki 知识：
{action_knowledge}

文件画像：
{profile}

知识路由表：
{route}

已路由 Wiki 协议动作基线如下。`03-动作清单` 必须忠实承接这些动作：
{wiki_protocol_action_baseline}

本阶段允许引用的审查协议页白名单如下，动作清单不得引用白名单之外的 `wiki/70-审查协议/` 页面：
{allowed_protocol_ref_lines}

本阶段 `03-动作清单` 必须包含以下 Wiki 协议字段，字段名不得省略：
{actions_required_field_lines}

请执行入口指引中的环节三：动作清单。

只输出 `03-动作清单` Markdown 内容。不得输出风险详情，不得生成最终报告。
动作来源白名单只能来自 `02-知识路由表` 中已启用或已调用的 Wiki 协议页；候选待确认、待确认、未启用、未调用、不适用的协议页不得生成动作。
`02-知识路由表` 中“是否必读”为“否”的协议页，只能作为后续质量门反查候选；除非该行执行状态明确为“已启用”或“已调用”，否则不得进入 `03-动作清单`。
动作必须来自上方“已路由 Wiki 协议动作基线”和入口指引允许的通用逐章动作；不得引用 `02-知识路由表` 未启用的审查协议页。
对上表中的动作，不得改写动作ID，不得改写 Wiki 动作名称，不得把多个 Wiki 动作压缩成一个泛化动作。
如果认为需要新增协议页或动作，必须在本阶段说明“需回到 02-知识路由表补充路由”，不得直接写入动作清单。
模型可以补充本文件读取范围、触发信号和执行优先级，但不能改变 Wiki 动作定义。
"""
    required_action_ids = list(dict.fromkeys(wiki_protocol_action_ids))
    if actions_path.is_file():
        actions = actions_path.read_text(encoding="utf-8")
        attempt_rows.append(
            {
                "stage": "03-动作清单",
                "attempt": 0,
                "max_tokens": 0,
                "prompt_hash": "resume",
                "output_hash": text_hash(actions),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "hit_limit": False,
                "protocol_ok": True,
                "issues": "",
            }
        )
    else:
        prompt_stats.append(("03-动作清单", len(actions_prompt), estimate_tokens(actions_prompt)))
        actions, usage = chat_stage(
            base_url,
            api_key,
            model,
            "03-动作清单",
            actions_prompt,
            max_tokens=10000,
            attempt_rows=attempt_rows,
            validator=combine_validators(
                lambda content: validate_wiki_protocol_output(
                    "03-动作清单",
                    content,
                    protocol_fields["03-动作清单"],
                    required_action_ids=required_action_ids,
                ),
                lambda content: validate_wiki_action_list(content, wiki_protocol_actions),
                lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
            ),
        )
        usages.append(("03-动作清单", usage))
        stage_file(actions_path, "03-动作清单", actions)
    action_tasks = parse_action_tasks(actions)
    action_list_ids = [task.action_id for task in action_tasks] or extract_action_ids_from_content(actions)
    review_stage_pages = filter_wiki_pages_by_allowed_actions(
        wiki_home,
        [*CORE_EXECUTION_PAGES, *routed_refs],
        action_list_ids,
    )

    action_exec_common_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请根据入口指引、文件画像、知识路由表和动作清单，执行环节四：逐动作执行。
风险判断只能来自已路由知识、动作清单和待审文件原文；不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
本阶段只执行 `03-动作清单` 中已经存在的动作ID，不得新增动作ID，不得新增审查协议页，不得把动作清单之外的事项写成候选风险。
报告和中间产物不得出现绝对路径。原文内容字段只能放待审文件原文。

文件画像：
{profile}

知识路由表：
{route}

逐动作执行必须覆盖动作清单中的全部动作ID：
{chr(10).join(f"- {action_id}" for action_id in action_list_ids)}

本阶段 `04-动作执行记录` 必须包含以下 Wiki 协议字段，字段名不得省略：
{action_exec_required_field_lines}
"""

    action_batches = build_action_batches(actions, numbered_text)
    action_batch_plan_rows = "\n".join(
        "| {batch_id} | {label} | {actions} | {checks} | {mode} | {chars} | {reason} |".format(
            batch_id=batch.batch_id,
            label=batch.label,
            actions="、".join(task.action_id for task in batch.tasks),
            checks=batch.check_count,
            mode=batch.input_mode,
            chars=batch.source_chars,
            reason=batch.reason,
        )
        for batch in action_batches
    )
    batch_records: list[str] = []
    review_pages: list[str] = []

    def split_batch_for_retry(batch: ActionBatch) -> list[ActionBatch]:
        retry_batches: list[ActionBatch] = []
        for task in batch.tasks:
            excerpt, input_mode = build_action_source_excerpt(numbered_text, [task])
            retry_batches.append(
                ActionBatch(
                    batch_id=f"{batch.batch_id}-{task.action_id}",
                    label=primary_action_section(task),
                    tasks=[task],
                    source_excerpt=excerpt,
                    reason=f"{batch.batch_id} 协议校验失败后单动作重跑",
                    input_mode=input_mode,
                    source_chars=len(excerpt),
                    check_count=action_check_count(task),
                )
            )
        return retry_batches

    def execute_action_batch(batch: ActionBatch, allow_split_retry: bool = True) -> None:
        batch_action_ids = [task.action_id for task in batch.tasks]
        batch_rel = (
            f"{output_rel_dir}/{stem}-04-动作执行记录-"
            f"{batch.batch_id}-{safe_filename_part(batch.label)}.md"
        )
        batch_path = output_dir / Path(batch_rel).name
        batch_knowledge, batch_pages = budget_wiki_pages_for_actions(
            wiki_home,
            review_stage_pages,
            batch_action_ids,
            char_budget=32000,
        )
        for page in batch_pages:
            if page not in review_pages:
                review_pages.append(page)
        batch_prompt = f"""{action_exec_common_prompt}

本次只执行动作组：{batch.batch_id} / {batch.label}
本阶段只输出本动作组的 `04-动作执行记录`，不生成最终报告。

本动作组可使用的 LLM Wiki 知识片段，只限 `02-知识路由表` 已启用知识和本组动作ID：
{batch_knowledge}

本动作组动作清单：
{format_action_batch_tasks(batch.tasks)}

本动作组必须逐项执行以下检查项，并在输出中保留 `检查项ID`。每个检查项都必须给出：执行状态、读取范围、原文位置或未命中原因、判断结果。不得合并省略。
{format_required_checklist(batch.tasks)}

本动作组对应原文窗口，已加行号：
{batch.source_excerpt}

请执行入口指引中的环节四：逐动作执行。

只输出本动作组 `04-动作执行记录` Markdown 内容。不要生成最终报告。
每个动作都必须有状态、读取范围、原文位置或未命中原因；命中和待确认动作必须形成候选风险或说明待确认原因。
每个 `检查项ID` 都必须在逐项检查结果中出现；如果某检查项未形成风险，也必须说明已读范围和不成立原因。
建议逐项检查结果包含动作ID、检查项ID、执行状态、读取范围、原文位置、判断结果；不得改写 `检查项ID`。
如原文窗口不足以判断，必须写 `待确认` 并说明缺少的原文范围，不得臆造结论。
"""
        stage_name = f"04-动作执行记录-{batch.batch_id}-{batch.label}"
        prompt_stats.append((stage_name, len(batch_prompt), estimate_tokens(batch_prompt)))
        if batch_pages:
            attempt_rows.append(
                {
                    "stage": stage_name,
                    "attempt": 0,
                    "max_tokens": 0,
                    "prompt_hash": "知识片段",
                    "output_hash": "、".join(batch_pages),
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "hit_limit": False,
                    "protocol_ok": True,
                    "issues": "",
                }
            )
        if batch_path.is_file():
            batch_record = batch_path.read_text(encoding="utf-8")
            validation_issues = combine_validators(
                lambda content, ids=batch_action_ids: validate_wiki_protocol_output(
                    "04-动作执行记录",
                    content,
                    protocol_fields["04-动作执行记录"],
                    required_action_ids=ids,
                ),
                lambda content, ids=batch_action_ids: validate_action_execution_coverage(content, ids),
                lambda content, tasks=batch.tasks: validate_required_check_coverage(content, tasks),
                lambda content: validate_no_unknown_action_ids(content, action_list_ids, "04-动作执行记录-动作组"),
                lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
                arithmetic_consistency_issues,
            )(batch_record)
            attempt_rows.append(
                {
                    "stage": stage_name,
                    "attempt": 0,
                    "max_tokens": 0,
                    "prompt_hash": "resume",
                    "output_hash": text_hash(batch_record),
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "hit_limit": False,
                    "protocol_ok": not validation_issues,
                    "issues": "；".join(validation_issues),
                }
            )
            if validation_issues:
                if allow_split_retry and len(batch.tasks) > 1:
                    attempt_rows.append(
                        {
                            "stage": stage_name,
                            "attempt": 0,
                            "max_tokens": 0,
                            "prompt_hash": "fallback-split",
                            "output_hash": "",
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "hit_limit": False,
                            "protocol_ok": False,
                            "issues": "协议校验失败，拆分为单动作重跑：" + "；".join(validation_issues),
                        }
                    )
                    for retry_batch in split_batch_for_retry(batch):
                        execute_action_batch(retry_batch, allow_split_retry=False)
                    return
                raise RuntimeError(f"{stage_name} resume output failed Wiki protocol check: " + "；".join(validation_issues))
        else:
            try:
                batch_record, usage = chat_stage(
                    base_url,
                    api_key,
                    model,
                    stage_name,
                    batch_prompt,
                    max_tokens=14000,
                    attempt_rows=attempt_rows,
                    max_retries=2,
                    validator=combine_validators(
                        lambda content, ids=batch_action_ids: validate_wiki_protocol_output(
                            "04-动作执行记录",
                            content,
                            protocol_fields["04-动作执行记录"],
                            required_action_ids=ids,
                        ),
                        lambda content, ids=batch_action_ids: validate_action_execution_coverage(content, ids),
                        lambda content, tasks=batch.tasks: validate_required_check_coverage(content, tasks),
                        lambda content: validate_no_unknown_action_ids(content, action_list_ids, "04-动作执行记录-动作组"),
                        lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
                        arithmetic_consistency_issues,
                    ),
                )
            except RuntimeError as exc:
                if allow_split_retry and len(batch.tasks) > 1 and "Wiki protocol check" in str(exc):
                    attempt_rows.append(
                        {
                            "stage": stage_name,
                            "attempt": 0,
                            "max_tokens": 0,
                            "prompt_hash": "fallback-split",
                            "output_hash": "",
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "hit_limit": False,
                            "protocol_ok": False,
                            "issues": "协议校验失败，拆分为单动作重跑：" + str(exc),
                        }
                    )
                    for retry_batch in split_batch_for_retry(batch):
                        execute_action_batch(retry_batch, allow_split_retry=False)
                    return
                raise
            usages.append((stage_name, usage))
            stage_file(batch_path, f"04-动作执行记录-{batch.batch_id}-{batch.label}", batch_record)
        stage_paths.append((stage_name, batch_rel))
        batch_records.append(f"## {batch.batch_id}：{batch.label}\n\n{batch_record}")

    for batch in action_batches:
        execute_action_batch(batch)

    merged_batches = "\n\n".join(batch_records)
    action_exec = f"""## 执行方式

本阶段按 `03-动作清单` 的必读章节和动作组分别执行。以下内容为各动作组已通过协议校验后的执行记录汇总；汇总过程不新增动作、不新增审查协议页、不新增风险事实。

{merged_batches}
"""
    merge_issues = combine_validators(
        lambda content: validate_wiki_protocol_output(
            "04-动作执行记录",
            content,
            protocol_fields["04-动作执行记录"],
            required_action_ids=required_action_ids,
        ),
        lambda content: validate_action_execution_coverage(content, action_list_ids),
        lambda content: validate_no_unknown_action_ids(content, action_list_ids, "04-动作执行记录"),
        lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
        arithmetic_consistency_issues,
    )(action_exec)
    attempt_rows.append(
        {
            "stage": "04-动作执行记录-合并",
            "attempt": 0,
            "max_tokens": 0,
            "prompt_hash": "engine-merge",
            "output_hash": text_hash(action_exec),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "hit_limit": False,
            "protocol_ok": not merge_issues,
            "issues": "；".join(merge_issues),
        }
    )
    if merge_issues:
        raise RuntimeError("04-动作执行记录-合并 failed Wiki protocol check: " + "；".join(merge_issues))
    stage_file(action_exec_path, "04-动作执行记录", action_exec)
    wiki_action_coverage_rows = build_wiki_action_coverage_rows(wiki_protocol_actions, actions, action_exec)
    deterministic_coverage_summary, deterministic_coverage_issues = deterministic_action_coverage_summary(
        action_list_ids,
        actions,
        action_exec,
    )
    review_knowledge, downstream_review_pages = budget_wiki_pages_for_actions(
        wiki_home,
        review_stage_pages,
        action_list_ids,
        char_budget=62000,
    )
    for page in downstream_review_pages:
        if page not in review_pages:
            review_pages.append(page)

    atomized_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节五：风险原子化。本阶段只处理动作执行记录中的候选风险。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。不得出现绝对路径。
本阶段不得新增动作ID、审查协议页或动作执行记录之外的风险来源；未在 `04-动作执行记录` 中形成候选风险的事项，不得写入原子风险清单。

风险原子化阶段 LLM Wiki 知识：
{review_knowledge}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

请执行入口指引中的环节五：风险原子化。

只输出 `05-原子风险清单` Markdown 内容。不要生成最终报告。
本阶段 `05-原子风险清单` 必须包含以下 Wiki 协议字段，字段名不得省略：
{atomized_required_field_lines}

必须按 LLM Wiki 风险原子化规则拆分候选风险；每个风险必须能反链来源动作和关联审查点。
每条原子风险的关联动作ID必须来自 `03-动作清单`，不得新增动作ID。
每条原子风险必须包含入口指引要求的全部字段；无法确定时填写 `是` 或 `待确认`。
"""
    prompt_stats.append(("05-原子风险清单", len(atomized_prompt), estimate_tokens(atomized_prompt)))
    atomized, usage = chat_stage(
        base_url,
        api_key,
        model,
        "05-原子风险清单",
        atomized_prompt,
        max_tokens=14000,
        attempt_rows=attempt_rows,
        validator=combine_validators(
            lambda content: validate_wiki_protocol_output(
                "05-原子风险清单",
                content,
                protocol_fields["05-原子风险清单"],
            ),
            lambda content: validate_no_unknown_action_ids(content, action_list_ids, "05-原子风险清单"),
            lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
            arithmetic_consistency_issues,
        ),
    )
    usages.append(("05-原子风险清单", usage))
    stage_file(atomized_path, "05-原子风险清单", atomized)

    quality_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请执行入口指引中的环节六：质量门反查。
不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。不得出现绝对路径。
本阶段只能反查前序产物，不得新增动作ID、审查协议页或新的风险事实。

质量门阶段 LLM Wiki 知识：
{review_knowledge}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

Wiki 协议动作执行覆盖检查：
| 协议页 | 动作ID | Wiki动作名称 | 进入动作清单 | 进入动作执行记录 | 动作语义一致 |
| --- | --- | --- | --- | --- | --- |
{wiki_action_coverage_rows}

执行器确定性动作覆盖校验：
{deterministic_coverage_summary}

质量门必须以“执行器确定性动作覆盖校验”为硬约束：如果确定性动作覆盖结论为通过，不得再写动作ID缺失、未进入动作清单、未进入动作执行记录、未执行或缺乏执行记录。质量门仍可检查证据链、风险原子化、横向专题产物和待复核项。

原子风险清单：
{atomized}

请执行入口指引中的环节六：质量门反查。

只输出 `06-质量门检查表` Markdown 内容。不要生成最终报告。
本阶段 `06-质量门检查表` 必须包含以下 Wiki 协议字段，字段名不得省略：
{quality_required_field_lines}

必须输出 `06-质量门检查表` 明细表，表头固定为：
| 质量门ID | 检查项 | 检查结果 | 发现问题 | 回退环节 | 处理动作 | 复查结果 |
字段名必须使用“质量门ID”，不得改写为“编号”“QM编号”或其他同义词。

必须检查入口指引列出的最低质量门；如风险数量偏低，必须执行异常低风险数量反查并记录反查范围和结论。
必须检查 Wiki 协议动作是否完整进入动作清单和动作执行记录；如存在缺失或语义不一致，不得写质量门通过。
如发现需要新增动作或协议页，必须输出“需回到 02/03 重跑”的质量门结论，不得在质量门阶段直接补动作或补风险。
"""
    prompt_stats.append(("06-质量门检查表", len(quality_prompt), estimate_tokens(quality_prompt)))
    quality, usage = chat_stage(
        base_url,
        api_key,
        model,
        "06-质量门检查表",
        quality_prompt,
        max_tokens=10000,
        attempt_rows=attempt_rows,
        validator=combine_validators(
            lambda content: validate_wiki_protocol_output(
                "06-质量门检查表",
                content,
                protocol_fields["06-质量门检查表"],
            ),
            lambda content: validate_no_unknown_action_ids(content, action_list_ids, "06-质量门检查表"),
            lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
            lambda content: validate_quality_not_contradict_action_coverage(
                content,
                action_list_ids,
                deterministic_coverage_issues,
            ),
            arithmetic_consistency_issues,
        ),
    )
    usages.append(("06-质量门检查表", usage))
    stage_file(quality_path, "06-质量门检查表", quality)

    report_prompt = f"""你是政府采购招标文件合规审查生产线的外部执行主体。

请基于已经生成并通过质量门检查的前六个中间产物，执行入口指引中的环节七：报告生成。
不得重新自由发挥，不得使用外部标注、标准答案、历史审查记录或执行器内置审查知识。
本阶段只能汇总 `05-原子风险清单` 和 `06-质量门检查表`，不得新增动作ID、审查协议页、风险事实或风险标题。
不得修改 LLM Wiki，不得对 LLM Wiki 运行维护命令。报告中不得出现绝对路径。

本次文件位置：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 输出目录：{PROMPT_OUTPUT_DIR}

文件画像：
{profile}

知识路由表：
{route}

动作清单：
{actions}

动作执行记录：
{action_exec}

原子风险清单：
{atomized}

质量门检查表：
{quality}

请执行入口指引中的环节七：报告生成，输出 `07-AI审查记录` Markdown 内容。

报告顶部只保留以下两项审查时间，不得输出 `类型::`、`状态::`、`审查日期::`、`审查时间::`、`审查人::`、`外部标注使用::`、`LLM Wiki修改::`、`LLM Wiki维护命令::`：
审查开始时间:: {PROMPT_REVIEW_START}
审查结束时间:: {PROMPT_REVIEW_END}

报告必须包含：
1. 审查摘要
2. 文件画像
3. 知识路由和动作状态
4. 风险点清单
5. 已审查未列风险
6. 待补证/待确认
7. 文件位置
8. 质量门结果

文件位置只能使用以下相对路径：
- 原始文件：{target}
- 抽取文本：{PROMPT_EXTRACT_REL}
- 文件画像：{PROMPT_PROFILE_REL}
- 知识路由表：{PROMPT_ROUTE_REL}
- 动作清单：{PROMPT_ACTIONS_REL}
- 动作执行记录：{PROMPT_ACTION_EXEC_REL}
- 原子风险清单：{PROMPT_ATOMIZED_REL}
- 质量门检查表：{PROMPT_QUALITY_REL}
- 运行记录：{PROMPT_RUN_REL}

每条风险必须保留：结论类型、风险等级、原文位置、原文内容、问题说明、关联动作ID、关联审查点、审查依据、修改建议、是否需要人工复核。
报告中的每个关联动作ID必须已经存在于 `03-动作清单`。
风险标题必须统一使用三级标题格式：### 风险 1：风险标题、### 风险 2：风险标题。
风险必须根据 LLM Wiki 风险审查点和待审文件原文判断；不得把执行器当作审查知识来源。
原文内容只能是原文摘录。
不得出现绝对路径。
"""
    prompt_stats.append(("07-AI审查记录", len(report_prompt), estimate_tokens(report_prompt)))
    report, usage = chat_stage(
        base_url,
        api_key,
        model,
        "07-AI审查记录",
        report_prompt,
        max_tokens=18000,
        attempt_rows=attempt_rows,
        validator=combine_validators(
            lambda content: validate_no_unknown_action_ids(content, action_list_ids, "07-AI审查记录"),
            lambda content: validate_action_protocol_refs_are_routed(content, routed_refs),
            arithmetic_consistency_issues,
        ),
    )
    usages.append(("07-AI审查记录", usage))
    if not report:
        print("pipeline returned empty report", file=sys.stderr)
        return 1
    review_end_time = local_now().strftime("%Y-%m-%d %H:%M:%S CST")
    report_replacements[PROMPT_REVIEW_END] = review_end_time
    report = replace_prompt_placeholders(report, report_replacements)
    report = remove_legacy_report_metadata(report)
    report = normalize_report_time_header(report, review_start_time, review_end_time)

    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
    risk_count = count_risks(report)

    usage_rows = "\n".join(
        f"| {name} | {usage.get('prompt_tokens', '')} | {usage.get('completion_tokens', '')} | {usage.get('total_tokens', '')} |"
        for name, usage in usages
    )
    prompt_size_rows = "\n".join(f"| {name} | {chars} | {tokens} |" for name, chars, tokens in prompt_stats)
    attempt_detail_rows = "\n".join(
        "| {stage} | {attempt} | {max_tokens} | {prompt_hash} | {output_hash} | {prompt_tokens} | {completion_tokens} | {total_tokens} | {hit_limit} | {protocol_ok} |".format(
            **row
        )
        for row in attempt_rows
    )
    stage_rows = "\n".join(f"| {name} | {path} |" for name, path in stage_paths)
    knowledge_pages = [*profile_pages, *route_pages, *action_pages, *review_pages]
    knowledge_rows = "\n".join(f"- {page}" for page in dict.fromkeys(knowledge_pages))

    run_record = f"""类型:: AI调度运行记录
状态:: 已完成
项目名称:: {stem}
执行日期:: {review_date}
执行时间:: {review_time}
执行人:: AI 审查
外部标注使用:: 否
LLM Wiki修改:: 否
LLM Wiki维护命令:: 否

# {stem} - AI调度运行记录

## 1. 基本信息

| 字段 | 内容 |
| --- | --- |
| 原始文件 | {target} |
| 抽取文本 | {extract_rel} |
| 文件画像 | {profile_rel} |
| 知识路由表 | {route_rel} |
| 动作清单 | {actions_rel} |
| 动作执行记录 | {action_exec_rel} |
| 原子风险清单 | {atomized_rel} |
| 质量门检查表 | {quality_rel} |
| 审查报告 | {report_rel} |
| 运行记录 | {run_rel} |
| 本次输出目录 | {output_rel_dir} |
| 文件分类 | {category} |
| 运行模式 | {EXECUTOR_NAME} SOP治理入口指引驱动流水线 |
| 运行时LLM | {model} |
| 风险点数量 | {risk_count} |

## 2. 执行边界

- 只读取本项目文件、本次指定招标文件，以及 config/hegui.yaml 中 wiki_home 指向的只读 LLM Wiki。
- 未读取外部标注、标准答案、人工批注或同一项目既有审查记录。
- 未修改 raw/ 下原始文件。
- 未修改 LLM Wiki。
- 未对 LLM Wiki 运行 ./lint、./ingest、./query 或其他维护命令。
- 已按 {SOP_DOC_REL} 执行边界运行；当 Wiki 协议无法有效驱动时，只记录反馈，不在 biz 侧补业务规则。

## 3. 中间产物清单

| 环节 | 产物 |
| --- | --- |
{stage_rows}

## 4. 调用知识清单

{knowledge_rows}

## 5. 模型交互说明

本次按入口指引执行分阶段知识驱动流水线。执行器按阶段装载知识包，避免每轮重复注入全量 LLM Wiki。

Prompt 大小估算：

| 阶段 | 字符数 | 粗略token估算 |
| --- | ---: | ---: |
{prompt_size_rows}

模型返回用量：

| 阶段 | prompt_tokens | completion_tokens | total_tokens |
| --- | ---: | ---: | ---: |
{usage_rows}

阶段调用复现信息：

| 阶段 | 尝试 | max_tokens | prompt_hash | output_hash | prompt_tokens | completion_tokens | total_tokens | 是否触达上限 | 协议校验通过 |
| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- | --- |
{attempt_detail_rows}

## 6. Wiki 协议动作执行覆盖

| 协议页 | 动作ID | Wiki动作名称 | 进入动作清单 | 进入动作执行记录 | 动作语义一致 |
| --- | --- | --- | --- | --- | --- |
{wiki_action_coverage_rows}

## 7. 04 动作批次计划

| 批次 | 标签 | 动作ID | 检查项数量 | 原文输入模式 | 原文窗口字符数 | 分批原因 |
| --- | --- | --- | ---: | --- | ---: | --- |
{action_batch_plan_rows}

## 8. 质量门结果

- 已按入口指引生成质量门检查表：{quality_rel}
- 风险点数量：{risk_count}。
- 外部标注使用：否。
- LLM Wiki修改：否。
- LLM Wiki维护命令：否。
"""
    run_path.write_text(run_record, encoding="utf-8")

    print(f"审查报告路径: {report_rel}")
    print(f"运行记录路径: {run_rel}")
    print(f"风险点数量: {risk_count}")
    print("是否使用外部标注: 否")
    print("是否修改 LLM Wiki: 否")
    print("是否对 LLM Wiki 运行维护命令: 否")
    print(f"中间产物目录: {output_rel_dir}")
    print("质量门结果: 已生成质量门检查表，详见审查报告和运行记录")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=EXECUTOR_NAME,
        description="按 hegui_cli.py 审查 SOP 治理边界审查一个政府采购招标文件。",
    )
    parser.add_argument(
        "--sop-check-only",
        action="store_true",
        help="只执行 SOP 运行前检查，不调用模型审查文件。",
    )
    parser.add_argument(
        "--resume-output",
        help="复用已有输出目录中的中间产物继续执行，目录必须位于本项目内。",
    )
    parser.add_argument("raw_file", nargs="?", help="待审查文件路径")
    return parser.parse_args(argv)


def resolve_target(raw_file: str, biz_home: Path, wiki_home: Path) -> tuple[str, Path] | str:
    target_path = Path(raw_file)
    if target_path.is_absolute():
        resolved = target_path.resolve()
        for root in (biz_home.resolve(), wiki_home.resolve()):
            try:
                rel = resolved.relative_to(root)
            except ValueError:
                continue
            if resolved.is_file():
                return rel.as_posix(), resolved
        return "target must be inside the business project or read-only LLM Wiki"

    if ".." in target_path.parts:
        return "target must be inside the business project or read-only LLM Wiki"
    biz_target = biz_home / target_path
    if biz_target.is_file():
        return target_path.as_posix(), biz_target
    wiki_target = wiki_home / target_path
    if wiki_target.is_file():
        return target_path.as_posix(), wiki_target
    return f"target not found: {raw_file}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    target_input = args.raw_file

    biz_home = Path(__file__).resolve().parent.parent.parent
    config_file = biz_home / "config/hegui.yaml"
    configured_wiki = read_simple_yaml_value(config_file, "wiki_home")
    wiki_setting = os.environ.get("HEGUI_WIKI_HOME") or configured_wiki
    wiki_home = Path(wiki_setting) if wiki_setting else biz_home.parent / "lab-hegui-llm"
    if not wiki_home.is_absolute():
        wiki_home = (biz_home / wiki_home).resolve()
    output_root = biz_home / "outputs"
    config_dir = biz_home / "config"

    sop_issues = sop_preflight(biz_home, wiki_home, config_dir)
    if sop_issues:
        for issue in sop_issues:
            print(f"SOP preflight failed: {issue}", file=sys.stderr)
        return 1
    if args.sop_check_only:
        print(f"{EXECUTOR_NAME} SOP preflight passed")
        print(f"SOP: {SOP_DOC_REL}")
        print(f"Wiki feedback: {WIKI_FEEDBACK_DOC_REL}")
        return 0
    if not args.raw_file:
        print("raw_file is required unless --sop-check-only is used", file=sys.stderr)
        return 2

    if not wiki_home.is_dir():
        print("wiki home not found", file=sys.stderr)
        return 1
    wiki_home = wiki_home.resolve()

    output_root.mkdir(parents=True, exist_ok=True)
    if not (config_dir / "config.toml").is_file() or not (config_dir / "auth.json").is_file():
        print("llm config not found: config/config.toml and config/auth.json are required", file=sys.stderr)
        return 1

    resolved_target = resolve_target(target_input, biz_home, wiki_home)
    if isinstance(resolved_target, str):
        print(resolved_target, file=sys.stderr)
        return 1
    target, target_path = resolved_target

    resume_output = Path(args.resume_output) if args.resume_output else None
    return direct_chat_review(target, target_path, biz_home, wiki_home, config_dir, resume_output=resume_output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
