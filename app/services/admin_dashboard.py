from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import ChatLog, PatientProfile
from app.services.drive import DriveGateway


KST = timezone(timedelta(hours=9))
DASHBOARD_MAX_CHAT_LOG_LIMIT = 200
DASHBOARD_DEFAULT_CHAT_LOG_LIMIT = 20
DASHBOARD_PAIR_RAW_LOG_LIMIT = 10_000


@dataclass
class _DashboardChatPair:
    patient: PatientProfile
    question: ChatLog | None = None
    answer: ChatLog | None = None


def _pair_chat_logs_for_dashboard(rows: list[tuple[PatientProfile, ChatLog]]) -> list[_DashboardChatPair]:
    pairs: dict[tuple[str, str, str], _DashboardChatPair] = {}
    job_to_question_keys: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    answer_rows: list[tuple[PatientProfile, ChatLog]] = []

    for patient, log in rows:
        if log.role == "assistant":
            answer_rows.append((patient, log))
            continue
        key = ("question", patient.patient_id, log.log_id)
        pairs[key] = _DashboardChatPair(patient=patient, question=log)
        if log.job_id:
            job_key = (patient.patient_id, log.kakao_user_id_hash, log.job_id)
            job_to_question_keys.setdefault(job_key, []).append(key)

    for patient, log in answer_rows:
        attached = False
        if log.job_id:
            job_key = (patient.patient_id, log.kakao_user_id_hash, log.job_id)
            for question_key in job_to_question_keys.get(job_key, []):
                pairs[question_key].answer = log
                attached = True
        if not attached and log.log_id.startswith("ANSWER_"):
            related_log_id = log.log_id.removeprefix("ANSWER_")
            question_key = ("question", patient.patient_id, related_log_id)
            pair = pairs.get(question_key)
            if pair is not None:
                pair.answer = log
                attached = True
        if not attached:
            pairs[("answer", patient.patient_id, log.log_id)] = _DashboardChatPair(
                patient=patient,
                answer=log,
            )

    return sorted(
        pairs.values(),
        key=lambda pair: (
            (pair.question.created_at if pair.question is not None else "")
            or (pair.answer.created_at if pair.answer is not None else ""),
            (pair.question.log_id if pair.question is not None else "")
            or (pair.answer.log_id if pair.answer is not None else ""),
        ),
        reverse=True,
    )


def _render_chat_log_pair(pair: _DashboardChatPair) -> dict[str, str]:
    question = pair.question
    answer = pair.answer
    created_at = question.created_at if question is not None else (answer.created_at if answer is not None else "")
    return {
        "created_at": created_at,
        "created_at_display": _format_kst_datetime(created_at),
        "patient_id": pair.patient.patient_id,
        "patient_name": pair.patient.name,
        "question": question.message if question is not None else "",
        "answer": answer.message if answer is not None else "",
        "question_created_at": question.created_at if question is not None else "",
        "answer_created_at": answer.created_at if answer is not None else "",
        "question_created_at_display": _format_kst_datetime(question.created_at if question is not None else ""),
        "answer_created_at_display": _format_kst_datetime(answer.created_at if answer is not None else ""),
        "answer_type": answer.message_type if answer is not None else "",
    }


def _format_kst_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def dashboard_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Medical Chatbot Admin</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #f3f4f6;
      --surface: #ffffff;
      --border: #e5e7eb;
      --border-light: #f3f4f6;
      --text-primary: #111827;
      --text-secondary: #6b7280;
      --text-muted: #9ca3af;
      --blue: #185fa5;
      --blue-dark: #0c447c;
      --green-bg: #eaf3de;
      --green-text: #3b6d11;
      --green-dark: #27500a;
      --red-bg: #fcebeb;
      --red-border: #f7c1c1;
      --red-text: #791f1f;
      --red-dark: #a32d2d;
      --amber-bg: #faeeda;
      --amber-text: #633806;
      --blue-bg: #e6f1fb;
      --blue-text: #0c447c;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text-primary);
    }
    body { background: var(--bg); min-height: 100vh; display: flex; }
    .sidebar {
      width: 200px;
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      position: fixed;
      top: 0;
      left: 0;
      height: 100vh;
      z-index: 10;
    }
    .sidebar-logo { padding: 18px 16px 14px; border-bottom: 1px solid var(--border-light); }
    .sidebar-logo-sub {
      font-size: 10px;
      font-weight: 500;
      color: var(--text-muted);
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .sidebar-logo-main { font-size: 15px; font-weight: 500; margin-top: 2px; }
    .sidebar-nav { padding: 10px 8px; flex: 1; }
    .nav-item {
      display: flex;
      align-items: center;
      gap: 9px;
      padding: 7px 10px;
      border-radius: 6px;
      color: var(--text-secondary);
      font-size: 13px;
      margin-bottom: 1px;
      text-decoration: none;
    }
    .nav-item:hover { background: #f9fafb; color: #374151; }
    .nav-item.active { background: var(--border-light); color: var(--text-primary); font-weight: 500; }
    .nav-item svg,
    .card-title svg,
    .btn svg,
    .status-icon svg,
    .action-btn svg,
    #logsLink svg {
      width: 16px;
      height: 16px;
      flex-shrink: 0;
      stroke: currentColor;
      fill: none;
      stroke-width: 1.75;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .sidebar-footer { padding: 12px 16px; border-top: 1px solid var(--border-light); }
    .sidebar-footer-label {
      font-size: 10px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: .06em;
      margin-bottom: 3px;
    }
    .sidebar-footer-val { font-size: 12px; color: var(--text-secondary); word-break: break-word; }
    .main-wrap {
      margin-left: 200px;
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }
    .topbar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .topbar-title { font-size: 14px; font-weight: 500; }
    .main-body {
      padding: 20px 24px 40px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      max-width: 1280px;
      width: 100%;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }
    .card-head {
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-light);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .card-title { font-size: 13px; font-weight: 500; display: flex; align-items: center; gap: 7px; }
    .card-title svg { color: var(--text-muted); }
    button, input, select { font: inherit; }
    .btn,
    .action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-size: 12px;
      min-height: 32px;
      padding: 6px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: #374151;
      cursor: pointer;
      white-space: nowrap;
    }
    .btn:hover,
    .action-btn:hover { background: #f9fafb; border-color: #c7ccd3; }
    .btn:disabled,
    .action-btn:disabled { opacity: .45; cursor: wait; }
    .btn-primary { background: var(--blue); border-color: var(--blue); color: #fff; }
    .btn-primary:hover { background: var(--blue-dark); border-color: var(--blue-dark); }
    .btn svg,
    .action-btn svg,
    #logsLink svg { width: 14px; height: 14px; }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      padding: 14px 16px;
    }
    .status-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border: 1px solid #f0f0f0;
      border-radius: 8px;
      background: #fafafa;
      min-width: 0;
    }
    .status-icon {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .icon-green { background: var(--green-bg); color: var(--green-text); }
    .icon-gray { background: var(--border-light); color: var(--text-secondary); }
    .status-name,
    .label { font-size: 11px; color: var(--text-secondary); margin-bottom: 2px; }
    .status-val,
    .value { font-size: 12px; font-weight: 500; color: var(--text-primary); word-break: break-word; }
    .status-val.ok { color: var(--green-text); }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 7px;
      flex-wrap: wrap;
      padding: 10px 16px;
      border-bottom: 1px solid var(--border-light);
      background: #fafafa;
    }
    .toolbar select,
    .toolbar input[type="number"],
    .toolbar input[type="date"] {
      font-size: 12px;
      padding: 6px 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text-primary);
    }
    .toolbar input[type="number"] { width: 64px; }
    .pager { display: flex; align-items: center; gap: 6px; margin-left: auto; }
    #chatPageLabel { font-size: 12px; color: var(--text-secondary); padding: 0 4px; white-space: nowrap; }
    #rangeLabel { font-size: 11px; color: var(--text-secondary); padding: 6px 16px 0; min-height: 16px; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; border: 1px solid #e2e8f0; }
    th {
      font-size: 11px;
      font-weight: 500;
      color: var(--text-secondary);
      padding: 8px 14px;
      border: 1px solid #e2e8f0;
      text-align: left;
      background: #fafafa;
      white-space: nowrap;
    }
    td {
      padding: 10px 14px;
      border: 1px solid #e2e8f0;
      vertical-align: top;
      color: var(--text-primary);
    }
    tbody tr:hover td { background: #f9fafb; }
    .td-time,
    .td-small { color: var(--text-secondary); font-size: 11px; white-space: nowrap; }
    .td-patient-name { font-size: 13px; font-weight: 500; }
    .td-patient-id { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
    .message { white-space: pre-wrap; word-break: break-word; min-width: 220px; max-width: 360px; color: var(--text-secondary); }
    .badge {
      display: inline-block;
      font-size: 11px;
      font-weight: 500;
      padding: 2px 8px;
      border-radius: 20px;
      white-space: nowrap;
    }
    .badge-answer { background: var(--amber-bg); color: var(--amber-text); }
    .badge-fail { background: var(--red-bg); color: var(--red-text); }
    .badge-gray { background: var(--border-light); color: var(--text-secondary); }
    .job-summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--border-light);
    }
    .js-card { border-radius: 8px; padding: 10px 12px; border: 1px solid var(--border); background: #fafafa; }
    .js-card.alert { border-color: var(--red-border); background: var(--red-bg); }
    .js-label { font-size: 11px; color: var(--text-secondary); margin-bottom: 5px; }
    .js-num { font-size: 22px; font-weight: 500; line-height: 1; color: var(--text-primary); }
    .js-num.red { color: var(--red-dark); }
    .js-num.green { color: var(--green-text); }
    .js-num.gray { color: var(--text-muted); }
    .action-row { display: flex; gap: 8px; flex-wrap: wrap; padding: 14px 16px; }
    .log-row { padding: 12px 16px; }
    #logsLink {
      font-size: 12px;
      color: var(--blue);
      display: inline-flex;
      align-items: center;
      gap: 5px;
      text-decoration: none;
    }
    #logsLink:hover { text-decoration: underline; }
    #result {
      margin: 12px 16px;
      background: #f9fafb;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 11px;
      color: var(--text-secondary);
      font-family: ui-monospace, "Cascadia Code", monospace;
      overflow: auto;
      max-height: 280px;
    }
    dialog {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      background: var(--surface);
      box-shadow: 0 4px 24px rgba(0, 0, 0, .1);
    }
    dialog::backdrop { background: rgba(17, 24, 39, .25); }
    dialog h2 { font-size: 14px; font-weight: 500; margin-bottom: 14px; }
    dialog .dialog-row { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
    dialog input[type="date"] {
      font-size: 12px;
      padding: 6px 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text-primary);
    }
    .empty-row { color: var(--text-muted); text-align: center; padding: 24px 14px; }
    @media (max-width: 780px) {
      body { display: block; }
      .sidebar {
        position: static;
        width: 100%;
        height: auto;
        border-right: none;
        border-bottom: 1px solid var(--border);
      }
      .sidebar-nav { display: flex; overflow-x: auto; padding: 8px; }
      .sidebar-footer { display: none; }
      .main-wrap { margin-left: 0; }
      .topbar { padding: 12px 16px; }
      .main-body { padding: 16px; }
      .pager { margin-left: 0; width: 100%; justify-content: flex-end; }
    }
  </style>
</head>
<body>
  <nav class="sidebar">
    <div class="sidebar-logo">
      <div class="sidebar-logo-sub">Medical</div>
      <div class="sidebar-logo-main">Chatbot Admin</div>
    </div>
    <div class="sidebar-nav">
      <a class="nav-item active" href="#status-section">
        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        대시보드
      </a>
      <a class="nav-item" href="#chat-section">
        <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        Chat log
      </a>
      <a class="nav-item" href="#callback-section">
        <svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
        Callback jobs
      </a>
      <a class="nav-item" href="#drive-section">
        <svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        Drive / Wiki
      </a>
      <a class="nav-item" href="#logs-section">
        <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        서버 로그
      </a>
    </div>
    <div class="sidebar-footer">
      <div class="sidebar-footer-label">환경</div>
      <div class="sidebar-footer-val" id="sidebarEnv">-</div>
    </div>
  </nav>

  <div class="main-wrap">
    <div class="topbar">
      <div class="topbar-title">대시보드</div>
      <button class="btn" onclick="loadAll()">
        <svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
        전체 새로고침
      </button>
    </div>
    <main class="main-body">
      <section class="card" id="status-section">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            시스템 상태
          </div>
        </div>
        <div id="status" class="status-grid"></div>
      </section>

      <section class="card" id="chat-section">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            채팅 기록
          </div>
        </div>
        <div class="toolbar">
          <select id="chatMode" onchange="changeChatMode()">
            <option value="recent">최근</option>
            <option value="range">기간</option>
          </select>
          <input id="limit" type="number" value="20" min="1" max="200">
          <button class="btn" onclick="openRangeDialog()">
            <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
            날짜 범위
          </button>
          <button class="btn btn-primary" onclick="loadChatLogs()">
            <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            검색
          </button>
          <button class="btn" onclick="downloadChatCsv()">
            <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            CSV 다운로드
          </button>
          <div class="pager">
            <button class="btn" onclick="moveChatPage(-1)" id="prevChatPage">
              <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
              이전
            </button>
            <span id="chatPageLabel">1</span>
            <button class="btn" onclick="moveChatPage(1)" id="nextChatPage">
              다음
              <svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
            </button>
          </div>
        </div>
        <div id="rangeLabel" class="label"></div>
        <div id="chatLogs" class="table-wrap"></div>
      </section>

      <section class="card" id="callback-section">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            콜백 작업 현황
          </div>
        </div>
        <div id="callbackJobs"></div>
      </section>

      <section class="card" id="drive-section">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            Drive / Wiki 관리
          </div>
        </div>
        <div class="action-row">
          <button class="action-btn" onclick="runAction('/admin/dashboard/actions/sync-drive-changes')">
            <svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
            Drive changes sync 실행
          </button>
          <button class="action-btn" onclick="runAction('/admin/dashboard/actions/sync-drive-full')">
            <svg viewBox="0 0 24 24"><polyline points="21 15 21 21 15 21"/><polyline points="3 9 3 3 9 3"/><line x1="21" y1="21" x2="14" y2="14"/><line x1="3" y1="3" x2="10" y2="10"/></svg>
            Full sync 실행
          </button>
          <button class="action-btn" onclick="runAction('/admin/dashboard/actions/cleanup-gemini-files')">
            <svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
            Gemini files cleanup 실행
          </button>
        </div>
      </section>

      <section class="card" id="logs-section">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            서버 로그
          </div>
        </div>
        <div class="log-row">
          <a id="logsLink" href="#" target="_blank" rel="noreferrer">
            <svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            Cloud Logging 실패 로그 열기
          </a>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <div class="card-title">
            <svg viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            실행 결과
          </div>
        </div>
        <pre id="result">준비됨</pre>
      </section>
    </main>
  </div>

  <dialog id="rangeDialog">
    <h2>날짜 범위</h2>
    <div class="dialog-row">
      <input id="startDate" type="date">
      <span style="font-size:12px;color:#6b7280;">~</span>
      <input id="endDate" type="date">
    </div>
    <div class="dialog-row">
      <button class="btn btn-primary" onclick="applyRangeDialog()">적용</button>
      <button class="btn" onclick="document.getElementById('rangeDialog').close()">닫기</button>
    </div>
  </dialog>
  <script>
    const chatState = { mode: 'recent', page: 1, startAt: '', endAt: '', hasPrev: false, hasNext: false };
    let currentChatLogItems = [];
    async function api(path, options = {}) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      return await res.json();
    }
    function setResult(value) {
      document.getElementById('result').textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    }
    const SVG = (d) => `<svg viewBox="0 0 24 24">${d}</svg>`;
    const dbIcon = () => SVG('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>');
    const driveIcon = () => SVG('<path d="M22 20H2l4-8 4 4 4-8 4 4z"/><path d="M6 12l-4 8"/><path d="M18 12l4 8"/>');
    const serverIcon = () => SVG('<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>');
    const bookIcon = () => SVG('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>');
    const folderIcon = () => SVG('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>');
    const circleIcon = () => SVG('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>');
    async function loadStatus() {
      const data = await api('/admin/dashboard/status');
      const envEl = document.getElementById('sidebarEnv');
      if (envEl) envEl.textContent = [data.app_env, data.app_name].filter(Boolean).join(' · ') || '-';
      const statusMap = {
        'Firestore': { val: data.firestore, icon: dbIcon(), ok: data.firestore === 'connected' },
        'Google Drive': { val: data.drive, icon: driveIcon(), ok: data.drive === 'connected' },
        'Callback worker': { val: data.callback_worker_mode, icon: serverIcon(), ok: false },
        'Wiki worker': { val: data.wiki_rebuild_worker_mode, icon: bookIcon(), ok: false },
        'Drive 루트 설정': { val: data.drive_root_config === 'id' ? 'ID 기준' : data.drive_root_config === 'name' ? '폴더명 기준' : data.drive_root_config, icon: folderIcon(), ok: false },
        'DB 이름': { val: data.firestore_database_id, icon: circleIcon(), ok: false },
      };
      document.getElementById('status').innerHTML = Object.entries(statusMap).map(([label, item]) => `
        <div class="status-item">
          <div class="status-icon ${item.ok ? 'icon-green' : 'icon-gray'}">${item.icon}</div>
          <div>
            <div class="status-name">${escapeHtml(label)}</div>
            <div class="status-val${item.ok ? ' ok' : ''}">${escapeHtml(item.val ?? '')}</div>
          </div>
        </div>`).join('');
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
    }
    function isoFromDate(value) {
      return value ? `${value}T00:00:00+09:00` : '';
    }
    function nextDateIso(value) {
      if (!value) return '';
      const [year, month, day] = value.split('-').map(Number);
      const date = new Date(Date.UTC(year, month - 1, day));
      date.setUTCDate(date.getUTCDate() + 1);
      const yyyy = date.getUTCFullYear();
      const mm = String(date.getUTCMonth() + 1).padStart(2, '0');
      const dd = String(date.getUTCDate()).padStart(2, '0');
      return `${yyyy}-${mm}-${dd}T00:00:00+09:00`;
    }
    function changeChatMode() {
      chatState.mode = document.getElementById('chatMode').value;
      chatState.page = 1;
      loadChatLogs();
    }
    function openRangeDialog() {
      document.getElementById('rangeDialog').showModal();
    }
    function applyRangeDialog() {
      const start = document.getElementById('startDate').value;
      const end = document.getElementById('endDate').value || start;
      chatState.mode = 'range';
      chatState.startAt = isoFromDate(start);
      chatState.endAt = nextDateIso(end);
      chatState.page = 1;
      document.getElementById('chatMode').value = 'range';
      document.getElementById('rangeDialog').close();
      loadChatLogs();
    }
    function moveChatPage(delta) {
      const nextPage = chatState.page + delta;
      if (nextPage < 1) return;
      if (delta < 0 && !chatState.hasPrev) return;
      if (delta > 0 && !chatState.hasNext) return;
      chatState.page = nextPage;
      loadChatLogs();
    }
    function updatePager(data) {
      chatState.mode = data.mode;
      chatState.page = data.page;
      chatState.hasPrev = data.has_prev;
      chatState.hasNext = data.has_next;
      document.getElementById('chatPageLabel').textContent = `${data.page} 페이지`;
      document.getElementById('prevChatPage').disabled = !data.has_prev;
      document.getElementById('nextChatPage').disabled = !data.has_next;
      document.getElementById('rangeLabel').textContent = data.mode === 'range' ? `${data.start_at} ~ ${data.end_at}` : '';
    }
    async function loadChatLogs() {
      const params = new URLSearchParams();
      chatState.mode = document.getElementById('chatMode').value;
      params.set('mode', chatState.mode);
      params.set('page', String(chatState.page));
      params.set('limit', document.getElementById('limit').value.trim() || '20');
      if (chatState.mode === 'range') {
        if (chatState.startAt) params.set('start_at', chatState.startAt);
        if (chatState.endAt) params.set('end_at', chatState.endAt);
      }
      const data = await api('/admin/dashboard/chat-logs?' + params.toString());
      currentChatLogItems = data.items || [];
      updatePager(data);
      const rows = currentChatLogItems.length
        ? currentChatLogItems.map(row => `<tr>
            <td class="td-time">${escapeHtml(row.question_created_at_display)}</td>
            <td class="td-time">${escapeHtml(row.answer_created_at_display)}</td>
            <td>
              <div class="td-patient-name">${escapeHtml(row.patient_name)}</div>
              <div class="td-patient-id">${escapeHtml(row.patient_id)}</div>
            </td>
            <td class="message">${escapeHtml(row.question)}</td>
            <td class="message">${escapeHtml(row.answer)}</td>
          </tr>`).join('')
        : '<tr><td colspan="5" class="empty-row">표시할 채팅 기록이 없습니다.</td></tr>';
      document.getElementById('chatLogs').innerHTML = `<table>
        <thead><tr>
          <th>질문 시간</th>
          <th>답변 시간</th>
          <th>환자</th>
          <th>질문</th>
          <th>답변</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }
    function csvCell(value) {
      let text = String(value ?? '');
      if (/^[=+\\-@\\t\\r]/.test(text)) text = "'" + text;
      return `"${text.replace(/"/g, '""')}"`;
    }
    function downloadChatCsv() {
      const headers = ['question_created_at', 'answer_created_at', 'patient_id', 'patient_name', 'question', 'answer'];
      const rows = currentChatLogItems.map(row => [
        row.question_created_at_display,
        row.answer_created_at_display,
        row.patient_id,
        row.patient_name,
        row.question,
        row.answer,
      ]);
      const csv = String.fromCharCode(0xfeff) + [headers, ...rows].map(row => row.map(csvCell).join(',')).join('\\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `chat_logs_page_${chatState.page}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }
    async function loadCallbackJobs() {
      const data = await api('/admin/dashboard/callback-jobs');
      const alertStatuses = new Set(['FAILED', 'EXPIRED']);
      const summaryHtml = Object.entries(data.counts).map(([k, v]) => {
        const isAlert = alertStatuses.has(k);
        const numClass = k === 'CALLBACK_SENT' ? 'green' : isAlert && v > 0 ? 'red' : 'gray';
        return `<div class="js-card${isAlert && v > 0 ? ' alert' : ''}">
          <div class="js-label">${escapeHtml(k)}</div>
          <div class="js-num ${numClass}">${escapeHtml(v)}</div>
        </div>`;
      }).join('');
      const rows = data.recent_failed_jobs.length
        ? data.recent_failed_jobs.map(row => `<tr>
            <td class="td-small">${escapeHtml(row.job_id)}</td>
            <td class="td-small">${escapeHtml(row.patient_id)}</td>
            <td><span class="badge badge-fail">${escapeHtml(row.status)}</span></td>
            <td class="td-small">${escapeHtml(row.retry_count)}/${escapeHtml(row.max_attempts)}</td>
            <td class="td-small">${escapeHtml(row.last_failure_code)}</td>
            <td class="td-small">${escapeHtml(row.updated_at)}</td>
          </tr>`).join('')
        : '<tr><td colspan="6" class="empty-row">최근 실패한 callback job이 없습니다.</td></tr>';
      document.getElementById('callbackJobs').innerHTML = `<div class="job-summary">${summaryHtml}</div>
        <div class="table-wrap"><table>
          <thead><tr>
            <th>job</th>
            <th>환자</th>
            <th>상태</th>
            <th>retry</th>
            <th>failure</th>
            <th>updated</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>`;
    }
    async function loadLogsLink() {
      const data = await api('/admin/dashboard/logs-link');
      document.getElementById('logsLink').href = data.url || '#';
    }
    async function runAction(path) {
      const buttons = document.querySelectorAll('button');
      buttons.forEach(button => button.disabled = true);
      try {
        const data = await api(path, { method: 'POST' });
        setResult(data);
        await loadAll();
      } catch (err) {
        setResult(String(err));
      } finally {
        buttons.forEach(button => button.disabled = false);
      }
    }
    async function loadAll() {
      try {
        await Promise.all([loadStatus(), loadChatLogs(), loadCallbackJobs(), loadLogsLink()]);
      } catch (err) {
        setResult(String(err));
      }
    }
    loadAll();
  </script>
</body>
</html>"""


@dataclass
class AdminDashboardService:
    settings: Settings
    repository: MedicalRepository
    drive_gateway: DriveGateway | None = None

    def status(self) -> dict[str, object]:
        drive_ok = False
        if self.drive_gateway is not None:
            try:
                root_id = (self.settings.google_drive_root_folder_id or "").strip()
                if root_id:
                    self.drive_gateway.get_file(root_id)
                else:
                    self.drive_gateway.find_folders_by_name(self.settings.google_drive_root_folder_name)
                drive_ok = True
            except Exception:
                drive_ok = False
        return {
            "firestore": "connected",
            "drive": "connected" if drive_ok else "unavailable",
            "app_env": self.settings.app_env,
            "app_name": self.settings.app_name,
            "firestore_project_id": self.settings.firestore_project_id or "",
            "firestore_database_id": self.settings.firestore_database_id,
            "callback_worker_mode": self.settings.callback_worker_mode,
            "wiki_rebuild_worker_mode": self.settings.wiki_rebuild_worker_mode,
            "drive_root_config": "id" if self.settings.google_drive_root_folder_id else "name",
        }

    def chat_logs(self, *, mode: str, start_at: str, end_at: str, limit: int, page: int) -> dict[str, object]:
        safe_mode = mode if mode in {"recent", "range"} else "recent"
        safe_limit = max(1, min(limit, DASHBOARD_MAX_CHAT_LOG_LIMIT))
        safe_page = max(1, page)
        query_start = start_at if safe_mode == "range" else ""
        query_end = end_at if safe_mode == "range" else ""
        rows = self.repository.list_chat_logs_for_dashboard(
            start_at=query_start,
            end_at=query_end,
            limit=DASHBOARD_PAIR_RAW_LOG_LIMIT,
            offset=0,
        )
        pairs = _pair_chat_logs_for_dashboard(rows)
        start_index = (safe_page - 1) * safe_limit
        page_pairs = pairs[start_index:start_index + safe_limit + 1]
        items = [_render_chat_log_pair(pair) for pair in page_pairs[:safe_limit]]
        return {
            "items": items,
            "mode": safe_mode,
            "limit": safe_limit,
            "page": safe_page,
            "has_prev": safe_page > 1,
            "has_next": len(page_pairs) > safe_limit,
            "start_at": query_start,
            "end_at": query_end,
        }

    def callback_jobs(self, *, limit: int = 20) -> dict[str, object]:
        safe_limit = max(1, min(limit, 100))
        failed_jobs = []
        for job in self.repository.list_recent_failed_callback_jobs(limit=safe_limit):
            failure = job.last_failure
            failed_jobs.append(
                {
                    "job_id": job.job_id,
                    "patient_id": job.patient_id,
                    "status": job.status,
                    "retry_count": job.retry_count,
                    "max_attempts": job.max_attempts,
                    "last_failure_code": failure.code if failure else "",
                    "updated_at": job.updated_at,
                    "created_at": job.created_at,
                }
            )
        return {
            "counts": self.repository.count_callback_jobs_by_status(),
            "recent_failed_jobs": failed_jobs,
        }

    def logs_link(self) -> dict[str, str]:
        project_id = self.settings.firestore_project_id or ""
        service_name = os.getenv("K_SERVICE") or self.settings.app_name
        query = (
            'resource.type="cloud_run_revision"\n'
            f'resource.labels.service_name="{service_name}"\n'
            "severity>=ERROR"
        )
        url = "https://console.cloud.google.com/logs/query;query=" + quote(query, safe="")
        if project_id:
            url += f"?project={quote(project_id, safe='')}"
        return {"url": url}
