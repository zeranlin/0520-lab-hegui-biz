#!/usr/bin/env python3
"""Local demo server for the government procurement review workspace."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


BIZ_HOME = Path(__file__).resolve().parent.parent
RAW_HOME = BIZ_HOME / "raw"
UPLOAD_HOME = RAW_HOME / "演示上传"
OUTPUT_HOME = BIZ_HOME / "outputs"
STATIC_HOME = Path(__file__).resolve().parent / "static"
CLI_PATH = BIZ_HOME / "agents/hegui-agent/hegui_cli.py"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

STAGE_DEFINITIONS = [
    ("01", "文件画像", "硬门槛字段已抽取，未见字段已标注"),
    ("02", "知识路由表", "必读知识和命中知识均有调用原因"),
    ("03", "动作清单", "必做动作完整，动作均有来源知识"),
    ("04", "动作执行记录", "每个动作均有状态和已读范围"),
    ("05", "原子风险清单", "候选风险已拆成可单独整改的问题"),
    ("06", "质量门检查表", "无未执行必做动作，无缺少证据风险"),
    ("07", "AI审查记录", "报告能反链画像、路由、动作和质量门"),
    ("08", "运行记录", "中间产物和质量门结果可追溯"),
]


@dataclass
class ReviewJob:
    id: str
    target: str
    status: str = "queued"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    returncode: int | None = None
    logs: list[str] = field(default_factory=list)
    output_dir: str = ""
    error: str = ""


JOBS: dict[str, ReviewJob] = {}
JOBS_LOCK = threading.Lock()


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, payload: str, status: int = 200, content_type: str = "text/plain") -> None:
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def safe_rel_path(raw_path: str, root: Path) -> Path | None:
    raw_path = unquote(raw_path).strip("/")
    if not raw_path:
        return None
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def list_raw_files() -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for path in sorted(RAW_HOME.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            rel = path.relative_to(BIZ_HOME).as_posix()
            files.append(
                {
                    "path": rel,
                    "name": path.name,
                    "category": path.parent.relative_to(RAW_HOME).as_posix(),
                    "suffix": path.suffix.lower(),
                    "size": str(path.stat().st_size),
                }
            )
    return files


def file_payload(path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(BIZ_HOME).as_posix(),
        "name": path.name,
        "category": path.parent.relative_to(RAW_HOME).as_posix(),
        "suffix": path.suffix.lower(),
        "size": str(path.stat().st_size),
    }


def safe_upload_name(filename: str) -> str:
    filename = Path(filename).name.strip()
    filename = re.sub(r"[\\/:*?\"<>|]+", "_", filename)
    if not filename:
        filename = f"upload-{int(time.time())}.docx"
    return filename


def parse_multipart_file(body: bytes, content_type: str) -> tuple[str, bytes]:
    match = re.search(r"boundary=(.+)", content_type)
    if not match:
        raise ValueError("上传请求缺少 boundary")
    boundary = match.group(1).strip().strip('"').encode("utf-8")
    for part in body.split(b"--" + boundary):
        if b'Content-Disposition:' not in part or b'name="file"' not in part:
            continue
        header_blob, _, content = part.partition(b"\r\n\r\n")
        disposition = header_blob.decode("utf-8", "replace")
        filename_match = re.search(r'filename="([^"]+)"', disposition)
        if not filename_match:
            raise ValueError("上传文件缺少文件名")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if content.endswith(b"--"):
            content = content[:-2]
            if content.endswith(b"\r\n"):
                content = content[:-2]
        return filename_match.group(1), content
    raise ValueError("未读取到上传文件")


def save_upload(body: bytes, content_type: str) -> dict[str, str]:
    filename, content = parse_multipart_file(body, content_type)
    safe_name = safe_upload_name(filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("仅支持 .pdf、.docx、.txt、.md")
    if not content:
        raise ValueError("上传文件为空")
    UPLOAD_HOME.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_HOME / safe_name
    if target.exists():
        stem = target.stem
        target = UPLOAD_HOME / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
    with target.open("wb") as file:
        file.write(content)
    return file_payload(target)


def append_log(job: ReviewJob, line: str) -> None:
    with JOBS_LOCK:
        job.logs.append(line.rstrip())
        if len(job.logs) > 1200:
            job.logs = job.logs[-1200:]


def run_review(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.status = "running"
    command = ["python3", str(CLI_PATH), job.target]
    append_log(job, f"$ {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            cwd=BIZ_HOME,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_log(job, line)
            match = re.search(r"(?:中间产物目录|批量输出目录|审查报告路径):\s*(outputs/[^\s]+)", line)
            if match:
                output_dir = match.group(1)
                if "-审查报告.md" in output_dir:
                    output_dir = str(Path(output_dir).parent).replace("\\", "/")
                with JOBS_LOCK:
                    job.output_dir = output_dir
        returncode = process.wait()
        with JOBS_LOCK:
            job.returncode = returncode
            job.status = "completed" if returncode == 0 else "failed"
            job.ended_at = time.time()
    except Exception as exc:  # pragma: no cover - demo diagnostic path
        append_log(job, f"error: {exc}")
        with JOBS_LOCK:
            job.returncode = 1
            job.status = "failed"
            job.error = str(exc)
            job.ended_at = time.time()


def start_job(target: str) -> ReviewJob:
    target_path = safe_rel_path(target, BIZ_HOME)
    if not target_path or not target_path.is_file() or target_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("待审查文件不存在或格式不支持")
    try:
        rel_target = target_path.relative_to(BIZ_HOME).as_posix()
    except ValueError as exc:
        raise ValueError("待审查文件必须位于项目目录内") from exc
    job = ReviewJob(id=uuid.uuid4().hex[:12], target=rel_target)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_review, args=(job.id,), daemon=True).start()
    return job


def find_latest_output_for_target(target: str) -> str:
    stem = Path(target).stem
    candidates = sorted(
        OUTPUT_HOME.rglob(f"{stem}-审查报告.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return ""
    return candidates[0].parent.relative_to(BIZ_HOME).as_posix()


def find_running_output_for_target(target: str, started_at: float) -> str:
    stem = Path(target).stem
    candidates = []
    for path in OUTPUT_HOME.rglob(f"{stem}*"):
        if not path.is_file():
            continue
        if path.stat().st_mtime + 3 < started_at:
            continue
        if any(marker in path.name for marker in ("抽取文本", "01-文件画像", "审查报告", "AI调度运行记录")):
            candidates.append(path.parent)
    if not candidates:
        return ""
    latest = sorted(set(candidates), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)[0]
    return latest.relative_to(BIZ_HOME).as_posix()


def job_payload(job: ReviewJob) -> dict[str, object]:
    output_dir = job.output_dir
    if job.status == "running" and not output_dir:
        output_dir = find_running_output_for_target(job.target, job.started_at)
    if job.status == "completed" and not output_dir:
        output_dir = find_latest_output_for_target(job.target)
    return {
        "id": job.id,
        "target": job.target,
        "status": job.status,
        "startedAt": job.started_at,
        "endedAt": job.ended_at,
        "returncode": job.returncode,
        "logs": job.logs,
        "outputDir": output_dir,
        "error": job.error,
    }


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def find_stage_file(output_dir: Path, code: str) -> Path | None:
    patterns = {
        "01": "*-01-文件画像.md",
        "02": "*-02-知识路由表.md",
        "03": "*-03-动作清单.md",
        "04": "*-04-动作执行记录.md",
        "05": "*-05-原子风险清单.md",
        "06": "*-06-质量门检查表.md",
        "07": "*-审查报告.md",
        "08": "*-AI调度运行记录.md",
    }
    matches = sorted(output_dir.glob(patterns[code]))
    return matches[0] if matches else None


def parse_value(text: str, labels: list[str]) -> str:
    for label in labels:
        patterns = [
            rf"^\s*[-*]?\s*\*\*{re.escape(label)}\*\*\s*[：:]\s*(.+)$",
            rf"^\s*[-*]?\s*{re.escape(label)}\s*[：:]\s*(.+)$",
            rf"^\s*{re.escape(label)}::\s*(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.MULTILINE)
            if match:
                return match.group(1).strip()
    return ""


def parse_risks(report: str) -> list[dict[str, str]]:
    matches = list(
        re.finditer(
            r"^#{2,3}\s+(?:风险\s*)?(\d+)[.．、：:]?\s*(.+)$",
            report,
            flags=re.MULTILINE,
        )
    )
    risks: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
        block = report[start:end]
        risk = {
            "id": f"RISK-{int(match.group(1)):03d}",
            "title": match.group(2).strip(),
            "level": parse_value(block, ["风险等级"]),
            "position": parse_value(block, ["原文位置"]),
            "action": parse_value(block, ["关联动作ID", "来源动作ID"]),
            "reviewPoint": parse_value(block, ["关联审查点"]),
            "needReview": parse_value(block, ["是否需要人工复核", "是否需人工复核"]),
            "suggestion": parse_value(block, ["修改建议"]),
            "body": block.strip(),
        }
        risk["module"] = classify_risk_module(risk)
        risk["disposition"] = classify_disposition(risk)
        risks.append(risk)
    return risks


def classify_risk_module(risk: dict[str, str]) -> str:
    text = f"{risk.get('title', '')} {risk.get('action', '')} {risk.get('reviewPoint', '')}"
    module_keywords = [
        ("合同履约", ("合同", "付款", "验收", "履约", "违约", "交接")),
        ("评分办法", ("评分", "评审", "分值", "人员", "证书", "业绩", "荣誉", "方案", "社保")),
        ("采购需求", ("需求", "技术参数", "服务范围", "人员配置", "响应时限", "用户需求", "核心产品")),
        ("公平竞争", ("歧视", "限制", "排斥", "特定", "资质", "认证", "地域", "供应商")),
        ("基本合规", ("公告", "预算", "最高限价", "采购方式", "资格", "中小企业", "联合体", "分包")),
    ]
    for module, keywords in module_keywords:
        if any(keyword in text for keyword in keywords):
            return module
    return "基本合规"


def classify_disposition(risk: dict[str, str]) -> str:
    level = risk.get("level", "")
    if "高" in level:
        return "必须修改"
    if "待确认" in risk.get("body", "") or "是" in risk.get("needReview", ""):
        return "需人工复核"
    if "中" in level:
        return "建议修改"
    return "建议关注"


def build_expert_modules(risks: list[dict[str, str]]) -> list[dict[str, object]]:
    modules = [
        ("基本合规", "公告、预算、采购方式、资格条件"),
        ("公平竞争", "差别歧视、特定资质、地域限制"),
        ("评分办法", "分值闭合、人员评分、供应商能力"),
        ("采购需求", "技术参数、服务范围、响应要求"),
        ("合同履约", "付款、验收、违约、模板一致性"),
    ]
    result = []
    for name, scope in modules:
        module_risks = [risk for risk in risks if risk.get("module") == name]
        high = [risk for risk in module_risks if "高" in risk.get("level", "")]
        pending = [risk for risk in module_risks if "是" in risk.get("needReview", "")]
        if high:
            status = "有重大风险"
            verdict = f"发现 {len(high)} 项高风险，建议先完成整改再发布。"
        elif module_risks:
            status = "有风险"
            verdict = f"发现 {len(module_risks)} 项需处理事项，建议纳入整改清单。"
        elif pending:
            status = "待核验"
            verdict = "存在需人工确认事项，建议补证后复核。"
        else:
            status = "未见异常"
            verdict = "本模块未见进入风险清单的问题。"
        result.append(
            {
                "name": name,
                "scope": scope,
                "status": status,
                "riskCount": len(module_risks),
                "highRiskCount": len(high),
                "verdict": verdict,
            }
        )
    return result


def build_release_advice(risks: list[dict[str, str]], quality: dict[str, object], stage_count: int) -> dict[str, str]:
    high_count = sum(1 for risk in risks if "高" in risk.get("level", ""))
    failed_quality = int(quality.get("failed") or 0)
    pending_quality = int(quality.get("pending") or 0)
    if stage_count < len(STAGE_DEFINITIONS) or failed_quality:
        return {
            "status": "暂缓发布",
            "tone": "danger",
            "reason": "审查产物或质量门未完成，当前不宜形成发布意见。",
        }
    if high_count:
        return {
            "status": "修改后发布",
            "tone": "warning",
            "reason": f"存在 {high_count} 项高风险，建议完成必须修改项后再发布。",
        }
    if pending_quality or any("是" in risk.get("needReview", "") for risk in risks):
        return {
            "status": "需人工复核",
            "tone": "review",
            "reason": "存在需人工确认事项，建议补证或复核后定稿。",
        }
    if risks:
        return {
            "status": "建议优化后发布",
            "tone": "notice",
            "reason": "未见高风险，但仍有建议修改事项。",
        }
    return {
        "status": "可发布",
        "tone": "ok",
        "reason": "质量门通过且未解析到风险清单事项。",
    }


def parse_quality(quality_text: str) -> dict[str, object]:
    rows = []
    for line in quality_text.splitlines():
        if not line.startswith("|") or "---" in line or "质量门ID" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3:
            rows.append(
                {
                    "id": cells[0],
                    "item": cells[1],
                    "result": cells[2],
                    "issue": cells[3] if len(cells) > 3 else "",
                }
            )
    failed = [row for row in rows if "未通过" in row["result"]]
    pending = [row for row in rows if "待确认" in row["result"]]
    return {"rows": rows, "failed": len(failed), "pending": len(pending)}


def build_result(output_rel: str) -> dict[str, object]:
    output_dir = safe_rel_path(output_rel, BIZ_HOME)
    if not output_dir or not output_dir.is_dir():
        raise ValueError("输出目录不存在")

    stages = []
    stage_texts: dict[str, str] = {}
    for code, name, gate in STAGE_DEFINITIONS:
        path = find_stage_file(output_dir, code)
        text = read_text(path) if path else ""
        stage_texts[code] = text
        stages.append(
            {
                "code": code,
                "name": name,
                "gate": gate,
                "status": "completed" if path else "missing",
                "path": path.relative_to(BIZ_HOME).as_posix() if path else "",
            }
        )

    report = stage_texts["07"]
    profile = stage_texts["01"]
    report_path = find_stage_file(output_dir, "07")
    fallback_project = output_dir.name
    if report_path:
        fallback_project = report_path.stem.removesuffix("-审查报告")
    quality = parse_quality(stage_texts["06"])
    risks = parse_risks(report)
    high_count = sum(1 for risk in risks if "高" in risk["level"])
    review_count = sum(1 for risk in risks if "是" in risk["needReview"])
    stage_complete_count = sum(1 for stage in stages if stage["status"] == "completed")
    return {
        "outputDir": output_dir.relative_to(BIZ_HOME).as_posix(),
        "project": parse_value(f"{report}\n{profile}", ["项目名称", "文件名称"]) or fallback_project,
        "buyer": parse_value(f"{report}\n{profile}", ["采购人"]),
        "method": parse_value(f"{report}\n{profile}", ["采购方式", "文件类型"]),
        "category": parse_value(f"{report}\n{profile}", ["采购品目", "采购标的", "标的属性"]),
        "budget": parse_value(f"{report}\n{profile}", ["预算金额", "最高限价"]),
        "stages": stages,
        "risks": risks,
        "quality": quality,
        "expertModules": build_expert_modules(risks),
        "releaseAdvice": build_release_advice(risks, quality, stage_complete_count),
        "summary": {
            "riskCount": len(risks),
            "highRiskCount": high_count,
            "manualReviewCount": review_count,
            "stageCompleteCount": stage_complete_count,
        },
        "artifacts": {
            code: {
                "name": name,
                "content": stage_texts[code],
                "path": next((stage["path"] for stage in stages if stage["code"] == code), ""),
            }
            for code, name, _ in STAGE_DEFINITIONS
        },
    }


def list_outputs() -> list[dict[str, object]]:
    reports = sorted(OUTPUT_HOME.rglob("*-审查报告.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    rows = []
    for report_path in reports[:80]:
        text = read_text(report_path)
        output_dir = report_path.parent.relative_to(BIZ_HOME).as_posix()
        rows.append(
            {
                "outputDir": output_dir,
                "report": report_path.relative_to(BIZ_HOME).as_posix(),
                "project": parse_value(text, ["项目名称"]) or report_path.stem.removesuffix("-审查报告"),
                "riskCount": len(parse_risks(text)),
                "mtime": report_path.stat().st_mtime,
            }
        )
    return rows


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/files":
            json_response(self, {"files": list_raw_files()})
            return
        if parsed.path == "/api/outputs":
            json_response(self, {"outputs": list_outputs()})
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                payload = job_payload(job) if job else None
            if not payload:
                json_response(self, {"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            json_response(self, payload)
            return
        if parsed.path == "/api/result":
            output_dir = parse_qs(parsed.query).get("outputDir", [""])[0]
            try:
                json_response(self, build_result(output_dir))
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        path = "index.html" if parsed.path in {"/", ""} else parsed.path.strip("/")
        static_path = safe_rel_path(path, STATIC_HOME)
        if not static_path or not static_path.is_file():
            text_response(self, "Not found", HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html" if static_path.suffix == ".html" else "text/css" if static_path.suffix == ".css" else "application/javascript"
        text_response(self, read_text(static_path), content_type=content_type)

    def do_POST(self) -> None:
        if self.path == "/api/upload":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = save_upload(self.rfile.read(length), self.headers.get("Content-Type", ""))
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, {"file": payload}, HTTPStatus.CREATED)
            return
        if self.path != "/api/jobs":
            json_response(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            job = start_job(str(data.get("target") or ""))
        except (json.JSONDecodeError, ValueError) as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        json_response(self, job_payload(job), HTTPStatus.CREATED)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), DemoHandler)
    print("合规审查 demo 已启动: http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
