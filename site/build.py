#!/usr/bin/env python3
"""Build the static GitHub Pages dashboard for xray-bench.

    python3 site/build.py [--results DIR] [--charts DIR] [--out DIR] [--lang all|en|ru]

Reads the benchmark JSON files and the committed reference charts, then writes
two self-contained pages sharing one asset tree:

    site/dist/index.html             (English, lang="en")
    site/dist/ru/index.html          (Russian, lang="ru")
    site/dist/assets/style.css
    site/dist/assets/charts/*.svg    (English reference charts)
    site/dist/assets/charts/ru/*.svg (Russian charts, when charts/ru exists)

Python standard library only.  The output is deterministic: no build timestamps,
no random data, no externally loaded fonts or images.  The only script is the
small inline language router in each page's head.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

# --------------------------------------------------------------------------
# project constants

REPO_URL = "https://github.com/pshakhmin/xray-bench"
README_EN_URL = f"{REPO_URL}/blob/master/README.en.md"
README_RU_URL = f"{REPO_URL}/blob/master/README.md"
WORKFLOW_URL = f"{REPO_URL}/blob/master/.github/workflows/bench.yml"
ACTIONS_URL = f"{REPO_URL}/actions/workflows/bench.yml"
CI_RESULTS_URL = f"{REPO_URL}/tree/master/ci-results"

CHART_FILES = (
    "throughput-rtt0.svg",
    "throughput-rtt50.svg",
    "connections.svg",
    "topology.svg",
)

# Same colours as charts/make_charts.py.  Unbihex carries the amber accent.
COLOR = {
    "xray": "#0e7490",
    "singbox": "#7c3aed",
    "unbihex_stock": "#b45309",
    "unknown": "#6b7280",
}

BENCH_KEYS = ("down1", "down8", "up1", "ttfb", "conns")
PHASES = ("down1", "down8", "up1", "ttfb", "conns")
EVIDENCE_MIN_DROP_THRESHOLD = 0.5
EVIDENCE_LOSS_MULTIPLIER = 3.0
EVIDENCE_BACKLOG_FRACTION = 0.5
INPUT_ORDER = ("xray", "singbox", "unbihex-server", "unbihex-cli")
INPUT_NAMES = {
    "xray": "Xray",
    "singbox": "sing-box",
    "unbihex-server": "Unbihex server",
    "unbihex-cli": "Unbihex CLI",
}
ENGINE_NAMES = {"xray": "Xray", "singbox": "sing-box", "unbihex": "Unbihex"}
ENGINE_ORDER = {"xray": 0, "singbox": 1, "unbihex": 2}

SITE_URL = "https://pshakhmin.github.io/xray-bench/"
LANGS = ("en", "ru")
LANG = "en"

# --------------------------------------------------------------------------
# message catalog
#
# Every visible string lives here, once per language.  _msg() registers both
# translations together, so a missing language is a build error rather than a
# silent English leak.  Templates may contain HTML and {placeholders}; callers
# pass already-escaped data for anything that comes from JSON.

CATALOG = {lang: {} for lang in LANGS}


def _msg(key: str, en: str, ru: str) -> None:
    CATALOG["en"][key] = en
    CATALOG["ru"][key] = ru


def set_lang(lang: str) -> None:
    global LANG
    LANG = lang if lang in LANGS else "en"


def t(key: str, **values) -> str:
    template = CATALOG[LANG][key]
    return template.format(**values) if values else template


PLURALS = {
    "en": {
        "files": ("file", "files"),
        "result_files": ("result file", "result files"),
        "throughput_files": ("throughput file", "throughput files"),
        "scenarios": ("scenario", "scenarios"),
        "phases": ("phase", "phases"),
    },
    "ru": {
        "files": ("файл", "файла", "файлов"),
        "result_files": ("файл результата", "файла результатов", "файлов результатов"),
        "throughput_files": ("файл throughput", "файла throughput", "файлов throughput"),
        "scenarios": ("сценарий", "сценария", "сценариев"),
        "phases": ("фаза", "фазы", "фаз"),
    },
}


def p(count, key: str) -> str:
    """Pick the plural form for the current language."""
    number = abs(int(count))
    forms = PLURALS[LANG][key]
    if LANG == "ru":
        if number % 10 == 1 and number % 100 != 11:
            return forms[0]
        if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
            return forms[1]
        return forms[2]
    return forms[0] if number == 1 else forms[1]


def phase_label(key: str) -> str:
    return t(f"phase.{key}")


# page shell, meta, header
_msg("meta.title", "xray-bench — proxy engine benchmark",
     "xray-bench — бенчмарк прокси-движков")
_msg(
    "meta.description",
    "A reproducible proxy-engine benchmark: Xray-core, sing-box and Unbihex on "
    "loopback or a shaped WAN-like link, with throughput, TTFB, connection "
    "rate, CPU, validity and qdisc/socket evidence.",
    "Воспроизводимый бенчмарк прокси-движков: Xray-core, sing-box и Unbihex на "
    "loopback или шейпованном WAN-канале — throughput, TTFB, скорость "
    "соединений, CPU, валидность и счётчики qdisc/сокетов.",
)
_msg("page.skip", "Skip to content", "Перейти к содержимому")
_msg("lang.label", "Language", "Язык")
_msg("lang.en", "EN", "EN")
_msg("lang.ru", "RU", "RU")
_msg("lang.en_title", "English version", "Английская версия")
_msg("lang.ru_title", "Russian version", "Русская версия")

# hero
_msg("hero.kicker", "proxy engine benchmark", "бенчмарк прокси-движков")
# Proper nouns: identical in both languages.  Each name keeps its trailing
# middot so a wrapped line never starts with a separator.
HERO_TITLE = (
    '<span class="h1-name">Xray '
    '<span class="sep" aria-hidden="true">·</span></span> '
    '<span class="h1-name">sing-box '
    '<span class="sep" aria-hidden="true">·</span></span> '
    '<span class="h1-name">Unbihex</span>'
)
_msg("hero.title", HERO_TITLE, HERO_TITLE)
_msg(
    "hero.lede",
    "Xray-core, sing-box and Unbihex measured along SOCKS → REALITY/XHTTP → "
    "engine server → sink: throughput, time to first byte, connection rate and "
    "CPU cost, with the binary hashes, kernel, link profile and qdisc/socket "
    "evidence recorded for every run.",
    "Xray-core, sing-box и Unbihex на пути SOCKS → REALITY/XHTTP → сервер "
    "движка → sink: пропускная способность, TTFB, скорость соединений и "
    "затраты CPU — с хешами бинарников, ядром, профилем канала и счётчиками "
    "qdisc/сокетов для каждого прогона.",
)
_msg(
    "hero.note",
    "Protocol-layer measurements on loopback, optionally shaped to a WAN-like "
    "link. Compare cells within one run — not with a real network link.",
    "Измерения на уровне протокола через loopback, при желании — с шейпингом "
    "под WAN-канал. Сравнивайте ячейки внутри одного прогона, а не с реальным "
    "каналом.",
)
_msg("hero.links_aria", "Project and data links", "Ссылки на проект и данные")
_msg("link.repo", "Repository", "Репозиторий")
_msg("link.readme_en", "README (English)", "README (English)")
_msg("link.readme_ru", "README (Русский)", "README (Русский)")
_msg("link.workflow", "CI workflow", "CI-воркфлоу")
_msg("link.raw", "raw ci-results/", "исходные ci-results/")
_msg("hero.spec_aria", "Bench at a glance", "Бенч в двух словах")
_msg("spec.path.label", "Path", "Путь")
_msg("spec.path.value", "SOCKS → REALITY / XHTTP → sink",
     "SOCKS → REALITY / XHTTP → sink")
_msg("spec.link.label", "Link", "Канал")
_msg("spec.link.value", "WAN-like or loopback · recorded per run",
     "WAN-подобный или loopback · фиксируется для прогона")
_msg("spec.measures.label", "Measures", "Метрики")
_msg("spec.measures.value", "throughput · TTFB · conn/s · CPU · evidence",
     "throughput · TTFB · conn/s · CPU · evidence")
_msg("spec.rule.label", "Rule", "Правило")
_msg("spec.rule.value", "compare within one run", "сравнивать внутри одного прогона")

# section numbers, titles and ledes
_msg("section.01.number", "01", "01")
_msg("section.02.number", "02", "02")
_msg("section.03.number", "03", "03")
_msg("section.04.number", "04", "04")
_msg("section.05.number", "05", "05")
_msg("section.01.title", "Latest CI run", "Последний прогон CI")
_msg(
    "section.01.lede",
    "The newest contiguous set of result files. One JSON per measured "
    "scenario × RTT; if a scenario was rerun, the newest file wins. Invalid "
    "results are marked, not hidden.",
    "Свежая серия результатов: файлы идут подряд, без разрывов. Один JSON на сценарий × RTT; если "
    "сценарий перезапускали, побеждает новый файл. Невалидные результаты "
    "помечаются, а не скрываются.",
)
_msg("section.02.title", "Results", "Результаты")
_msg(
    "section.02.lede",
    "Charts and tables come from the same JSON. Invalid results are marked, "
    "not hidden; missing values are shown as — and are never guessed.",
    "Графики и таблицы — из одного JSON. Невалидные результаты помечаются, а "
    "не скрываются; пропуски показаны как — и никогда не додумываются.",
)
_msg("section.03.title", "Cost & evidence", "Ресурсы и счётчики")
_msg(
    "section.03.lede",
    "System CPU, RAM and engine RSS from each result, plus qdisc and socket "
    "counters. Missing counters are shown as —.",
    "CPU системы, RAM и RSS движка из каждого результата, плюс счётчики qdisc "
    "и сокетов. Отсутствующие счётчики показаны как —.",
)
_msg("section.04.title", "16-core reference", "Референс на 16 ядрах")
_msg(
    "section.04.lede",
    "The committed charts, kept in their original visual language. Use them to "
    "compare engines, not to compare a CI runner with this machine.",
    "Закоммиченные графики в исходной визуальной подаче. Сравнивайте движки, а "
    "не CI-раннер с этой машиной.",
)
_msg("section.05.title", "Caveats", "Оговорки")
_msg(
    "section.05.lede",
    "Read these before quoting any number. The bench answers “which engine is "
    "faster under identical conditions”, not “how fast will my link be”.",
    "Прочитайте до того, как цитировать любую цифру. Бенч отвечает на вопрос "
    "«какой движок быстрее в одинаковых условиях», а не «насколько быстрым "
    "будет мой канал».",
)

# phases
_msg("phase.down1", "↓1 download", "↓1 скачивание")
_msg("phase.down8", "↓8 download", "↓8 скачивание")
_msg("phase.up1", "↑1 upload", "↑1 отдача")
_msg("phase.ttfb", "TTFB", "TTFB")
_msg("phase.conns", "Connection rate", "Скорость соединений")

# suffixes for generated scenario names
_msg("suffix.release", " (release)", " (релиз)")
_msg("suffix.stock", " (stock)", " (релиз)")
_msg("suffix.xhttp", " (XHTTP)", " (XHTTP)")

# run strip, validity, cards
_msg("run.chip.scenarios", "{word}", "{word}")
_msg("run.chip.results", "{word}", "{word}")
_msg("run.chip.date", "{date} run date", "{date} — дата прогона")
_msg("run.chip.vcpu", "{n} vCPU", "{n} vCPU")
_msg("run.mixed_pair", "mixed pair", "смешанная пара")
_msg("validity.unknown_none",
     "No validity flag was recorded in these result files (older format).",
     "В этих файлах нет флага валидности (старый формат).")
_msg("validity.unknown_partial",
     "Only {known} of {total} result files carry a validity flag (older format).",
     "Флаг валидности записан не везде: {known} из {total} (старый формат).")
_msg("validity.valid_one", "Result marked valid.", "Результат помечен как валидный.")
_msg("validity.valid_all",
     "All {n} {files} are marked valid.",
     "Все файлы ({n}) помечены как валидные.")
_msg("validity.badge.valid", "valid", "валиден")
_msg("validity.badge.invalid", "invalid", "невалиден")
_msg("validity.badge.unchecked", "unchecked", "не проверено")
_msg("validity.unchecked_aria", "Validity not recorded", "Валидность не записана")
_msg("validity.invalid_title", "Invalid: {reasons}", "Невалидно: {reasons}")
_msg("validity.flagged", "Flagged invalid", "Помечено невалидным")
_msg("validity.count",
     "{flagged} of {total} result files flagged",
     "Помечено {flagged} из {total} файлов результатов")
_msg("validity.notice_title", "Flagged invalid.", "Помечено невалидным.")
_msg("validity.notice_body",
     "The numbers are still shown and marked; validity is a warning, not a filter.",
     "Цифры всё равно показаны и помечены; валидность — предупреждение, а не фильтр.")
_msg("validity.no_reason", "No reason recorded.", "Причина не записана.")
_msg("profile.dash", "profile —", "профиль —")
_msg("profile.netem_only", "netem only", "только netem")
_msg("link.delay_one_way", "{values} ms one-way", "{values} мс в одну сторону")
_msg("link.per_rtt", " (per RTT)", " (на полный RTT)")
_msg("link.jitter", " · jitter {values} ms", " · джиттер {values} мс")
_msg("link.no_cap", "no cap", "без лимита")
_msg("link.card_title", "Link profile", "Профиль канала")
_msg("link.kv.profile", "Profile", "Профиль")
_msg("link.kv.delay", "Delay", "Задержка")
_msg("link.kv.loss", "Loss", "Потери")
_msg("link.kv.rate", "Rate cap", "Лимит полосы")
_msg("link.kv.mtu", "MTU", "MTU")
_msg("link.kv.gso", "GSO", "GSO")
_msg("link.kv.ports", "Ports", "Порты")
_msg("link.kv.queue", "Queue limit", "Лимит очереди")
_msg("link.delay_off", "off (RTT ≈ 0)", "выключена (RTT ≈ 0)")
_msg("link.delay_each_way", "{one} ms each way (≈ {rtt} ms RTT)",
     "{one} ms в каждую сторону (≈ {rtt} ms RTT)")
_msg("link.legacy_note",
     'Older result: no <code>link</code> profile was recorded, so this card '
     'falls back to the <code>netem</code> values.',
     'Старый результат: профиль <code>link</code> не записан, поэтому '
     'карточка использует значения <code>netem</code>.')
_msg("card.hardware", "Hardware", "Железо")
_msg("card.source", "Source", "Источник")
_msg("card.binaries", "Binaries", "Бинарники")
_msg("kv.cpu", "CPU", "CPU")
_msg("kv.nproc", "nproc", "nproc")
_msg("kv.kernel", "Kernel", "Ядро")
_msg("kv.memory", "Memory", "Память")
_msg("mem.free_total", "{free} free / {total} total", "{free} свободно / {total} всего")
_msg("mem.free_title", "{free} KiB free / {total} KiB total",
     "{free} KiB свободно / {total} KiB всего")
_msg("git.commit", "Commit", "Коммит")
_msg("git.branch", "Branch", "Ветка")
_msg("git.run_window", "Run window", "Окно прогона")
_msg("git.newest", "Newest result", "Свежий результат")
_msg("git.dirty", "dirty tree", "грязное дерево")
_msg("git.clean", "clean tree", "чистое дерево")
_msg("binaries.empty", "No binary metadata was recorded for this run.",
     "Для этого прогона не записаны метаданные бинарников.")
_msg("binaries.aria", "Binaries table", "Таблица бинарников")
_msg("binaries.caption", "Binaries recorded in the newest result file",
     "Бинарники из свежего файла результата")
_msg("binaries.col.binary", "Binary", "Бинарник")
_msg("binaries.col.version", "Version", "Версия")
_msg("binaries.col.sha", "SHA-256 (short)", "SHA-256 (кратко)")
_msg("binaries.label.version", "Version", "Версия")
_msg("binaries.label.sha", "SHA-256", "SHA-256")
_msg("input.unbihex_server", "Unbihex server", "Unbihex (сервер)")
_msg("input.unbihex_cli", "Unbihex CLI", "Unbihex CLI")

# run callout
_msg("run.callout.aria", "Comparability warning", "Предупреждение о сравнимости")
_msg("run.callout.title", "Only compare numbers within one run.",
     "Сравнивайте цифры только внутри одного прогона.")
_msg("run.callout.first",
     "This run recorded {n} vCPU. Published CI runs use 4-vCPU GitHub-hosted "
     "runners, and the committed reference used 16 cores.",
     "Этот прогон записал {n} vCPU. Публичные прогоны CI идут на "
     "раннерах GitHub с 4 vCPU, референс снимался на 16 ядрах.")
_msg("run.callout.first_default",
     "GitHub-hosted runners have 4 vCPU; the committed reference below used 16 cores.",
     "У раннеров GitHub 4 vCPU; референс ниже снимался на 16 ядрах.")
_msg("run.callout.body",
     "Absolute MB/s depends on the machine, the kernel and the socket buffers. "
     "These are protocol-layer numbers on a recorded loopback path, not a "
     "prediction for a real network link.",
     "Абсолютный MB/s зависит от машины, ядра и буферов сокетов. Это цифры "
     "уровня протокола на записанном loopback-пути, а не прогноз для реального "
     "канала.")

# run sources details
_msg("sources.summary", "Result files behind this run",
     "Файлы результатов этого прогона")
_msg("sources.scanned", "Scanned {n} JSON {files} in this directory.",
     "Просканировано JSON-файлов: {n}.")
_msg("sources.charted",
     "Charted {n} {result_files} from the latest contiguous run ({m} {throughput_files}).",
     "В графиках: {n} {result_files} из последней непрерывной серии "
     "({m} {throughput_files}).")
_msg("sources.superseded",
     "{n} earlier {files} in the same run were superseded by a newer file for "
     "the same scenario and RTT.",
     "Более ранние файлы в том же прогоне ({n}) перекрыты новыми для того же "
     "сценария и RTT.")
_msg("sources.older",
     "{n} {result_files} from earlier runs were not charted.",
     "Не попали в графики: {n} {result_files} из прошлых прогонов.")
_msg("sources.ignored",
     "{n} {files} without throughput metrics (for example DNS runs) were ignored.",
     "Пропущено файлов без метрик throughput (например, DNS): {n}.")
_msg("sources.invalid",
     "{n} {files} could not be read and were skipped: {names}.",
     "Не удалось прочитать файлов: {n}; пропущены: {names}.")
_msg("sources.unread", "{n} {files} could not be read.",
     "Не удалось прочитать файлов: {n}.")
_msg("sources.raw",
     'Raw JSON is committed to <a href="{results_url}">ci-results/</a> and '
     'uploaded as the <code>ci-results</code> artifact from the '
     '<a href="{actions_url}">bench workflow</a>.',
     'Исходный JSON лежит в <a href="{results_url}">ci-results/</a> и '
     'загружается артефактом <code>ci-results</code> из '
     '<a href="{actions_url}">бенч-воркфлоу</a>.')

# empty run state
_msg("empty.no_ci", "No CI results yet", "Результатов CI пока нет")
_msg("empty.title", "This build has no measured run to chart.",
     "В этой сборке нет измеренного прогона для графиков.")
_msg("empty.missing",
     "The results directory <code>ci-results/</code> is not present in this "
     "build. The page falls back to the committed 16-core reference below.",
     "Каталога <code>ci-results/</code> в этой сборке нет. Страница "
     "переходит к закоммиченному референсу на 16 ядрах ниже.")
_msg("empty.empty_dir",
     "The results directory is empty. The workflow writes one JSON file per "
     "measured scenario × RTT; until it runs, the committed reference below is "
     "the only data on this page.",
     "Каталог результатов пуст. Воркфлоу пишет один JSON на сценарий × RTT; "
     "пока он не отработал, единственные данные на странице — референс ниже.")
_msg("empty.not_directory", "The results path is not a directory.",
     "Путь результатов — не каталог.")
_msg("empty.no_bench",
     "The results directory has {n} JSON {files}, but none contain throughput "
     "metrics. The page falls back to the committed reference below.",
     "В каталоге результатов {n} JSON-файлов, но метрик throughput в них нет. "
     "Страница переходит к закоммиченному референсу ниже.")
_msg("empty.body",
     "The page still builds, and the reference charts and caveats below still "
     "apply. To preview a local directory:",
     "Страница всё равно собирается; референсные графики и оговорки ниже "
     "действуют. Локальный каталог можно посмотреть так:")

# results charts and tables
_msg("results.nothing_tag", "Nothing to chart", "Нечего строить")
_msg("results.nothing_title", "No measured scenarios in this build.",
     "В этой сборке нет измеренных сценариев.")
_msg("results.nothing_body",
     "The generator found no JSON files with throughput, TTFB or connection "
     "metrics. Reference charts and caveats are unaffected.",
     "Генератор не нашёл JSON с метриками throughput, TTFB или соединений. "
     "Референсные графики и оговорки не затронуты.")
_msg("results.sample",
     "Latest file per scenario × RTT. A dash means the metric is absent from the JSON.",
     "Последний файл на сценарий × RTT. Прочерк — метрики нет в JSON.")
_msg("results.table_hint", "Tables below scroll sideways.",
     "Таблицы ниже прокручиваются вбок.")
_msg("results.missing_rtt0",
     "No RTT ≈ 0 throughput results in this run. The 50 ms section below is shown instead.",
     "В этом прогоне нет throughput при RTT ≈ 0. Ниже показан раздел 50 ms.")
_msg("results.missing_rtt50",
     "No 50 ms results in this run. The CI probe skips RTT 50 ms when netem is "
     "unavailable on the runner.",
     "В этом прогоне нет результатов 50 ms. CI-проба пропускает RTT 50 ms, "
     "если netem недоступен на раннере.")
_msg("results.missing_ttfb", "No TTFB metric at RTT ≈ 0 in this run.",
     "В этом прогоне нет метрики TTFB при RTT ≈ 0.")
_msg("results.missing_conns", "No connection-rate metric at RTT ≈ 0 in this run.",
     "В этом прогоне нет метрики скорости соединений при RTT ≈ 0.")

_msg("chart.throughput.title", "Throughput at RTT ≈ 0",
     "Пропускная способность при RTT ≈ 0")
_msg("chart.throughput.sub", "Median MB/s through SOCKS · higher is better",
     "Медиана MB/s через SOCKS · больше — лучше")
_msg("chart.throughput.foot",
     "Medians over the recorded runs. MB/s as printed by the bench (bytes/1e6).",
     "Медианы по записанным прогонам. MB/s как печатает бенч (bytes/1e6).")
_msg("chart.rtt.title", "Single-stream download: RTT ≈ 0 vs 50 ms",
     "Скачивание в один поток: RTT ≈ 0 и 50 ms")
_msg("chart.rtt.sub", "Median MB/s · higher is better",
     "Медиана MB/s · больше — лучше")
_msg("chart.rtt.foot",
     "At 8 streams and 50 ms, netem itself is the bottleneck, so only ↓1 is "
     "compared at 50 ms.",
     "При 8 потоках и 50 ms узкое место — сам netem, поэтому на 50 ms "
     "сравнивается только ↓1.")
_msg("chart.ttfb.title", "Time to first byte", "Время до первого байта")
_msg("chart.ttfb.sub", "ms · bar = median, mark = p95 · lower is better",
     "ms · столбец — медиана, метка — p95 · меньше — лучше")
_msg("chart.ttfb.foot",
     "TTFB covers TCP to SOCKS, CONNECT, tunnel setup and one byte from the "
     "target. The mark is the p95 over the recorded runs.",
     "TTFB покрывает TCP до SOCKS, CONNECT, установку туннеля и один байт от "
     "цели. Метка — p95 по записанным прогонам.")
_msg("chart.conns.title", "Connection rate", "Скорость соединений")
_msg("chart.conns.sub", "conn/s · higher is better", "conn/s · больше — лучше")
_msg("chart.conns.foot",
     "Connections per second with four requests in flight, over the recorded runs.",
     "Соединений в секунду при четырёх запросах в полёте, по записанным прогонам.")
_msg("legend.aria", "Series", "Серии")

_msg("table.engine", "Engine / scenario", "Движок / сценарий")
_msg("table.throughput.aria", "Throughput table", "Таблица пропускной способности")
_msg("table.throughput.caption",
     "Medians at RTT ≈ 0. MB/s as printed by the bench (bytes/1e6); min–max in "
     "smaller type where recorded.",
     "Медианы при RTT ≈ 0. MB/s как печатает бенч (bytes/1e6); мин–макс "
     "мельче, где записан.")
_msg("table.rtt.aria", "Single-stream download table",
     "Таблица скачивания в один поток")
_msg("table.rtt.caption",
     "Single-stream download at RTT ≈ 0 and at 50 ms. MB/s as printed by the "
     "bench (bytes/1e6); min–max in smaller type where recorded.",
     "Скачивание в один поток при RTT ≈ 0 и 50 ms. MB/s как печатает бенч "
     "(bytes/1e6); мин–макс мельче, где записан.")
_msg("table.ttfb.aria", "Time to first byte table",
     "Таблица времени до первого байта")
_msg("table.ttfb.caption",
     "Time to first byte at RTT ≈ 0: median, p95, and the number of "
     "connections with errors.",
     "Время до первого байта при RTT ≈ 0: медиана, p95 и число соединений с "
     "ошибками.")
_msg("table.ttfb.median", "Median ms", "Медиана, ms")
_msg("table.ttfb.p95", "p95 ms", "p95 ms")
_msg("table.connections", "Connections", "Соединения")
_msg("table.errors", "Errors", "Ошибки")
_msg("table.conns.aria", "Connection rate table", "Таблица скорости соединений")
_msg("table.conns.caption",
     "Connection rate at RTT ≈ 0: connections per second, total connections, "
     "requests in flight, and errors.",
     "Скорость соединений при RTT ≈ 0: соединений в секунду, всего "
     "соединений, запросов в полёте и ошибок.")
_msg("table.conns.rate", "Conn/s", "Conn/s")
_msg("table.conns.inflight", "In flight", "В полёте")

# evidence
_msg("evidence.tooltip",
     "Drops include simulated loss and queue overflow. Drop rate = dropped / "
     "packets. Bottleneck threshold = max(3 x configured loss, 0.5%)",
     "Дропы включают смоделированные потери и переполнение очереди. Доля "
     "дропов = dropped / packets. Порог узкого места = max(3 × настроенные "
     "потери, 0.5%)")
_msg("evidence.netem_short", "netem bottleneck", "узкое место netem")
_msg("evidence.netem_long", "netem was the bottleneck", "узким местом был netem")
_msg("evidence.line",
     "qdisc {drops} drops · {rate} · {overlimits} overlimits · {retrans} retrans",
     "qdisc: дропы {drops} · {rate} · overlimits {overlimits} · retrans {retrans}")
_msg("evidence.line_empty", "qdisc — drops · — overlimits · — retrans",
     "qdisc: дропы — · overlimits — · retrans —")
_msg("evidence.loss_configured", "loss {rate}% as configured",
     "потери {rate}% по конфигу")
_msg("evidence.reason.overlimits", "overlimits {n} > 0", "overlimits {n} > 0")
_msg("evidence.reason.drop", "drop rate {rate}% > {threshold}% threshold{suffix}",
     "доля дропов {rate}% > порога {threshold}%{suffix}")
_msg("evidence.reason.drop_unknown", " (configured loss unknown)",
     " (настроенные потери неизвестны)")
_msg("evidence.reason.backlog", "backlog {n} {unit}>= 50% of limit {limit}",
     "backlog {n} {unit}>= 50% лимита {limit}")

# cost section
_msg("cost.missing", "No cost or evidence data in this build.",
     "В этой сборке нет данных о ресурсах и evidence.")
_msg("cost.table_hint", "Cost and evidence tables scroll sideways.",
     "Таблицы ресурсов и evidence прокручиваются вбок.")
_msg("cost.callout.aria", "Shaper warning", "Предупреждение шейпера")
_msg("cost.callout.title", "netem was the bottleneck.", "Узким местом был netem.")
_msg("cost.callout.intro", "qdisc evidence exceeded the bottleneck rule for:",
     "Счётчики qdisc превысили порог узкого места:")
_msg("cost.callout.item", "{drops} drops · {rate}; {reasons}",
     "дропы: {drops} · {rate}; {reasons}")
_msg("cost.callout.fallback", "threshold exceeded", "порог превышен")
_msg("cost.callout.more", "+{n} more phase(s)", "ещё фаз: {n}")
_msg("cost.callout.body",
     "Drops alone are not a bottleneck: the qdisc counter includes the "
     "configured simulated loss. This callout requires overlimits, a drop rate "
     "above the threshold, or a backlog at half the parsed queue limit.",
     "Дропы сами по себе — не узкое место: счётчик qdisc включает настроенные "
     "потери. Вызов срабатывает при overlimits, доле дропов выше порога или "
     "backlog на половине разобранного лимита очереди.")
_msg("cost.predate",
     "These result files predate the <code>sys</code> and <code>evidence</code> "
     "fields. The cost and evidence panels below show — for the missing counters.",
     "Эти файлы старше полей <code>sys</code> и <code>evidence</code>. Панели "
     "ниже показывают — для отсутствующих счётчиков.")
_msg("cost.cpu.aria", "CPU cost table", "Таблица затрат CPU")
_msg("cost.cpu.none",
     "No <code>sys.cpu</code> counters were recorded for this scenario (older "
     "result format).",
     "Для этого сценария нет счётчиков <code>sys.cpu</code> (старый формат).")
_msg("cost.cpu.caption",
     "System CPU counters. Busy cores = busy_ms / 1000 / wall_s. CPU % of wall "
     "= busy_ms / (wall_s × 1000 × nproc) × 100, computed only when wall time "
     "can be derived from a recorded transfer total or connection count.",
     "Счётчики CPU системы. Занятых ядер = busy_ms / 1000 / wall_s. CPU % "
     "времени = busy_ms / (wall_s × 1000 × nproc) × 100; считается только "
     "когда время можно вывести из записанного объёма передачи или числа "
     "соединений.")
_msg("cost.col.phase", "Phase", "Фаза")
_msg("cost.col.runs", "Runs", "Прогоны")
_msg("cost.col.busy", "Busy ms", "Занято, ms")
_msg("cost.col.wall", "Wall s", "Время, s")
_msg("cost.col.cores", "Busy cores", "Занятых ядер")
_msg("cost.col.cpu_pct", "CPU % wall", "CPU, % времени")
_msg("cost.col.user", "User ms", "User, ms")
_msg("cost.col.system", "System ms", "System, ms")
_msg("cost.col.softirq", "Softirq ms", "Softirq, ms")
_msg("cost.col.irq", "IRQ ms", "IRQ, ms")
_msg("cost.evidence.aria", "qdisc and socket evidence table",
     "Таблица счётчиков qdisc и сокетов")
_msg("cost.evidence.none",
     "No qdisc or socket evidence was recorded for this scenario (older result "
     "format).",
     "Для этого сценария нет счётчиков qdisc или сокетов (старый формат).")
_msg("cost.evidence.caption",
     "qdisc and socket counters as recorded per phase. Drop rate = dropped / "
     "packets and includes simulated loss. Bottleneck when overlimits > 0, "
     "drop rate > max(3 × configured loss, 0.5%), or backlog ≥ 50% of the "
     "parsed queue limit. When configured loss is unknown, 0.5% is used.",
     "Счётчики qdisc и сокетов по фазам. Доля дропов = dropped / packets, "
     "включая смоделированные потери. Узкое место при overlimits > 0, доле "
     "дропов > max(3 × настроенные потери, 0.5%) или backlog ≥ 50% "
     "разобранного лимита очереди. Если настроенные потери неизвестны, "
     "берётся 0.5%.")
_msg("cost.col.qdisc_drops", "qdisc drops", "Дропы qdisc")
_msg("cost.col.drop_rate", "Drop rate", "Доля дропов")
_msg("cost.col.overlimits", "Overlimits", "Overlimits")
_msg("cost.col.requeues", "Requeues", "Requeues")
_msg("cost.col.backlog", "Backlog pkt", "Backlog, pkt")
_msg("cost.col.retrans", "Sock retrans", "Ретрансмиты")
_msg("cost.col.sockets", "Sockets", "Сокеты")
_msg("cost.raw.summary", "Raw qdisc / socket counters (truncated)",
     "Сырые счётчики qdisc / сокетов (обрезано)")
_msg("cost.ram.chip", "RAM <strong>{free}</strong> {source} / {total}",
     "RAM <strong>{free}</strong> {source} / {total}")
_msg("cost.ram.empty", "RAM <strong>—</strong>", "RAM <strong>—</strong>")
_msg("cost.ram.machine", "machine start", "на старте машины")
_msg("cost.ram.free", "free min", "минимум свободно")
_msg("cost.rss.chip",
     "RSS peak · server <strong>{server}</strong> · client <strong>{client}</strong>",
     "RSS пик · сервер <strong>{server}</strong> · клиент <strong>{client}</strong>")
_msg("cost.rss.empty", "RSS peak <strong>—</strong>", "RSS пик <strong>—</strong>")
_msg("cost.h4.cpu", "CPU cost", "Затраты CPU")
_msg("cost.h4.evidence", "qdisc / socket evidence", "qdisc / сокеты")
_msg("cost.loss_note",
     "Configured loss is not recorded for at least one result; the drop-rate "
     "comparison uses the 0.5% fallback threshold.",
     "Настроенные потери записаны не для всех результатов; сравнение доли "
     "дропов использует запасной порог 0.5%.")

# reference section
_msg("ref.callout.aria", "Reference hardware note", "Замечание о референсном железе")
_msg("ref.callout.title", "16-core reference, not a CI runner.",
     "Референс на 16 ядрах, а не CI-раннер.")
_msg("ref.callout.body",
     "These are the four images committed in <code>charts/</code> and linked "
     "from both READMEs. They were taken with the wan profile: MTU 1500, "
     "offloads off, no loss, no rate cap, and the run clock starts before the "
     "connection is established. The values are duplicated in "
     "<code>charts/data.json</code> because <code>results/</code> is not committed.",
     "Это четыре изображения из <code>charts/</code>, на них ссылаются оба "
     "README. Сняты на профиле <code>wan</code>: MTU 1500, offloads выключены, "
     "без потерь и без лимита полосы; таймер прогона стартует до установки "
     "соединения. Значения продублированы в <code>charts/data.json</code>, "
     "потому что <code>results/</code> не коммитится.")
_msg("ref.meta.release", "Release", "Релиз")
_msg("ref.meta.base", "Base", "База")
_msg("ref.meta.date", "Date", "Дата")
_msg("ref.meta.machine", "Machine", "Машина")
_msg("ref.meta.path", "Path", "Путь")
_msg("ref.meta.rtt", "RTT", "RTT")
_msg("ref.meta.profile", "Profile", "Профиль")
_msg("ref.meta.profile_value", "wan · MTU 1500 · offloads off · no loss",
     "wan · MTU 1500 · offloads off · без потерь")
_msg("ref.value.cores", "{n} cores", "{n} ядер")
_msg("ref.value.rtt_method", "netem on the tunnel port only",
     "netem только на порт туннеля")
_msg("ref.story.falls", "At 50 ms {label} falls from {base} to {shaped} MB/s",
     "На 50 ms {label} падает с {base} до {shaped} MB/s")
_msg("ref.story.drop", ", a {ratio}× drop", " — в {ratio} раз")
_msg("ref.story.holds_both", ", while Xray holds {xray} and sing-box {singbox} MB/s",
     ", тогда как Xray держит {xray}, а sing-box — {singbox} MB/s")
_msg("ref.story.holds_xray", ", while Xray holds {xray} MB/s",
     ", тогда как Xray держит {xray} MB/s")
_msg("ref.story.holds_singbox", ", while sing-box holds {singbox} MB/s",
     ", тогда как sing-box держит {singbox} MB/s")
_msg("ref.story.rank",
     "On the RTT ≈ 0 run {label} has the lowest connection rate ({conns} conn/s) "
     "and the highest TTFB p95 ({ttfb} ms).",
     "На прогоне с RTT ≈ 0 у {label} меньше всего соединений в секунду "
     "({conns} conn/s) и самый высокий TTFB p95 ({ttfb} ms).")
_msg("ref.story.neutral",
     "On the RTT ≈ 0 run {label} records {conns} conn/s and {ttfb} ms TTFB p95.",
     "На прогоне с RTT ≈ 0 {label} показывает {conns} conn/s и TTFB p95 "
     "{ttfb} ms.")
_msg("ref.meta_missing",
     "Reference metadata (<code>charts/data.json</code>) is unavailable in this build.",
     "Метаданные референса (<code>charts/data.json</code>) в этой сборке "
     "недоступны.")
_msg("ref.no_svg", "No reference SVG could be copied from the charts directory.",
     "Из каталога графиков не удалось скопировать ни одного SVG.")
_msg("ref.sources.summary", "Source files behind the reference medians",
     "Файлы результатов, на которых построены медианы")
_msg("ref.sources.note",
     "These local result files are not committed to the repository; the "
     "medians above are copied into <code>charts/data.json</code>.",
     "Эти локальные файлы не закоммичены; медианы выше скопированы в "
     "<code>charts/data.json</code>.")
_msg("ref.charts_fallback",
     "Russian chart labels are unavailable in this build; the English charts are shown.",
     "Русские подписи к графикам в этой сборке недоступны; показаны английские.")
_msg("ref.fig.rtt0.caption",
     "Median MB/s for ↓1, ↓8 and ↑1 across the three engines.",
     "Медиана MB/s для ↓1, ↓8 и ↑1 по трём движкам.")
_msg("ref.fig.rtt0.alt",
     "Grouped bar chart of median throughput in MB/s at RTT close to zero for "
     "one and eight download streams and one upload stream, three engines.",
     "Сгруппированная столбчатая диаграмма медианной пропускной способности в "
     "MB/s при RTT около нуля: один и восемь потоков на скачивание и один на "
     "отдачу, три движка.")
_msg("ref.fig.rtt50.caption",
     "↓1 at RTT ≈ 0 and at 50 ms for all three engines.",
     "↓1 при RTT ≈ 0 и при 50 ms по трём движкам.")
_msg("ref.fig.rtt50.alt",
     "Grouped bar chart of single-stream download throughput at RTT close to "
     "zero and at 50 ms for the three engines.",
     "Сгруппированная столбчатая диаграмма скачивания в один поток при RTT "
     "около нуля и при 50 ms по трём движкам.")
_msg("ref.fig.connections.caption",
     "TTFB median with p95 whiskers (left) and connection rate (right).",
     "Медиана TTFB с усами p95 (слева) и скорость соединений (справа).")
_msg("ref.fig.connections.alt",
     "Two-panel chart at RTT close to zero. Left: time to first byte, median "
     "bar with p95 whisker, lower is better. Right: connections per second, "
     "higher is better. Unbihex 0.2.2 has the highest TTFB p95 and the lowest "
     "connection rate.",
     "Двухпанельная диаграмма при RTT около нуля. Слева: время до первого "
     "байта, столбец — медиана, ус — p95, меньше — лучше. Справа: соединений в "
     "секунду, больше — лучше. У Unbihex 0.2.2 самый высокий TTFB p95 и самая "
     "низкая скорость соединений.")
_msg("ref.fig.topology.caption", "Bench path; netem touches only the tunnel port.",
     "Путь бенча; netem действует только на порт туннеля.")
_msg("ref.fig.topology.alt",
     "Diagram of the bench path: bench client to engine client over SOCKS, "
     "engine client to engine server over REALITY or XHTTP with netem adding "
     "delay only on the tunnel port, then engine server to the sink. SOCKS and "
     "the path to the sink carry no added delay.",
     "Схема пути бенча: клиент бенча → клиент движка по SOCKS, клиент → сервер "
     "движка по REALITY или XHTTP, netem добавляет задержку только на порт "
     "туннеля, затем сервер → sink. SOCKS и путь до sink идут без добавленной "
     "задержки.")

_msg("run.unknown_suffix", "Their numbers are still shown.",
     "Их цифры всё равно показаны.")
_msg("run.missing_link",
     "{n} selected {files} {verb} no <code>link</code> profile; the header "
     "shows the newest recorded profile.",
     "У выбранных файлов ({n}) нет профиля <code>link</code>; в шапке — самый "
     "свежий записанный профиль.")
_msg("run.multiple_profiles",
     "Selected result files use more than one link profile ({profiles}); the "
     "header shows the newest recorded profile.",
     "Выбранные файлы используют несколько профилей канала ({profiles}); в "
     "шапке — самый свежий записанный профиль.")

# caveats
_msg("caveat.1.title", "A shaped path, still not a NIC",
     "Шейпер, но всё ещё не NIC")
_msg("caveat.1.body",
     "Current runs can emulate a WAN-like link: MTU 1500, offloads off and "
     "0.1% loss. The packets still traverse loopback, not a real NIC, driver or "
     "ISP path.",
     "Текущие прогоны умеют имитировать WAN-канал: MTU 1500, offloads выключены, "
     "потери 0.1%. Пакеты по-прежнему идут через loopback, а не через реальный "
     "NIC, драйвер или путь провайдера.")
_msg("caveat.2.title", "No bandwidth cap by default",
     "По умолчанию без ограничения полосы")
_msg("caveat.2.body",
     "link.rate_mbit = 0 means no cap. Delay, jitter, loss, MTU and offload "
     "settings are shaped; a default run is not a bandwidth-limited link.",
     "link.rate_mbit = 0 означает отсутствие лимита. Шейпинг задаёт задержку, "
     "джиттер, потери, MTU и offload; обычный прогон — не канал с ограниченной "
     "полосой.")
_msg("caveat.3.title", "50 ms shaper limit at 8 streams",
     "Предел шейпера: 50 ms, 8 потоков")
_msg("caveat.3.body",
     "At 8 streams and 50 ms the shaper itself can be the bottleneck. qdisc "
     "drops or overlimits are recorded; when they are above zero the cell "
     "describes netem, not the engine.",
     "При 8 потоках и 50 ms узким местом может стать сам шейпер. Дропы и "
     "overlimits qdisc записываются; если они больше нуля, ячейка описывает "
     "netem, а не движок.")
_msg("caveat.4.title", "Evidence is a host-side sample",
     "Evidence — выборка со стороны хоста")
_msg("caveat.4.body",
     "qdisc and socket counters come from this host's kernel. They can prove "
     "that the shaper limited a cell; they do not turn loopback into a real "
     "network path.",
     "Счётчики qdisc и сокетов берутся из ядра этого хоста. Они доказывают, что "
     "шейпер ограничил ячейку, но не превращают loopback в реальный сетевой "
     "путь.")
_msg("caveat.5.title", "No TUN mode", "Без TUN-режима")
_msg("caveat.5.body",
     "Unbihex's main mode is not measured here. TUN changes are covered by "
     "unit tests, not by this bench.",
     "Основной режим Unbihex здесь не измеряется. Изменения в TUN покрыты "
     "юнит-тестами, а не этим бенчем.")
_msg("caveat.6.title", "Competitors out of the box",
     "Конкуренты «из коробки»")
_msg("caveat.6.body",
     "The Go binaries run without sockopt tuning; Unbihex runs with its "
     "defaults. This compares what ships, not what is possible after tuning.",
     "Go-бинарники запускаются без sockopt-тюнинга, Unbihex — со своими "
     "настройками по умолчанию. Сравнивается то, что поставляется, а не то, "
     "что возможно после тюнинга.")
_msg("caveat.7.title", "CPU time is understated", "CPU-время занижено")
_msg("caveat.7.body",
     "System CPU busy time is recorded, but loopback remains syscall-heavy. "
     "CPU differences are smaller here than they would be on a real link.",
     "Время занятости system CPU записывается, но loopback остаётся насыщенным "
     "системными вызовами. Разница по CPU здесь меньше, чем была бы на реальном "
     "канале.")
_msg("caveat.8.title", "CI hardware is not the reference",
     "CI-железо — не референс")
_msg("caveat.8.body",
     "GitHub-hosted runners have 4 vCPU; the reference used 16 cores. Numbers "
     "are only comparable within one run, never across machines.",
     "У раннеров GitHub 4 vCPU, референс снимался на 16 ядрах. Цифры "
     "сравнимы только внутри одного прогона, никогда — между машинами.")
_msg("caveat.9.title", "REALITY dest is local", "REALITY dest локальный")
_msg("caveat.9.body",
     "The TLS site behind REALITY is on this machine. Probing a real remote "
     "site costs more than this bench shows.",
     "TLS-сайт за REALITY находится на этой же машине. Проверка реального "
     "удалённого сайта стоит дороже, чем показывает этот бенч.")

# footer
_msg("footer.line1",
     "<strong>xray-bench</strong> — a reproducible proxy-engine bench. Measured "
     "on loopback paths, published from JSON.",
     "<strong>xray-bench</strong> — воспроизводимый бенч прокси-движков. "
     "Измерения на loopback-путях, публикация из JSON.")
_msg("footer.built",
     'Built by <code>site/build.py</code>. One inline script routes the '
     'language; no tracking, no external fonts or assets.',
     'Собрано <code>site/build.py</code>. Язык выбирает один встроенный скрипт; '
     'без трекинга, внешних шрифтов и ресурсов.')
_msg("footer.note",
     "Absolute throughput is a protocol-layer measurement on a shaped loopback "
     "path, not a speed test of a real connection.",
     "Абсолютный throughput — измерение на уровне протокола через шейпованный "
     "loopback, а не тест скорости реального соединения.")

# A CI invocation runs its scenarios within a short window.  Results are
# committed and accumulate in ci-results/, so only the newest contiguous run is
# charted: files separated by more than six hours belong to another run.
RUN_GAP_SECONDS = 2 * 60 * 60

MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# small formatting helpers


def esc(value) -> str:
    return escape(str(value), quote=True)


def is_num(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def fmt_num(value, digits: int = 1, dash: str = "—") -> str:
    """Fixed decimals with trailing zeros removed; never 'None' or 'nan'."""
    if not is_num(value):
        return dash
    text = f"{float(value):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def fmt_int(value, dash: str = "—") -> str:
    if not is_num(value):
        return dash
    return str(int(round(float(value))))


def fmt_kb(value, dash: str = "—") -> str:
    """Human-readable RAM/RSS size from a KiB value."""
    if not is_num(value):
        return dash
    kib = float(value)
    if kib >= 1024 * 1024:
        return f"{kib / (1024 * 1024):.1f} GiB"
    if kib >= 1024:
        return f"{kib / 1024:.1f} MiB"
    return f"{kib:.0f} KiB"


def truncate_text(value, limit: int = 360) -> str:
    text = str(value).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def clamp_pct(value, maximum: float) -> float:
    if not is_num(value) or maximum <= 0:
        return 0.0
    return max(0.0, min(100.0, float(value) / maximum * 100.0))


def text_or_dash(value) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return text if text else "—"


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_dt(value) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_date(value) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d")


def tint(hex_color: str, amount: float = 0.42) -> str:
    """Mix a colour towards white, exactly as charts/make_charts.py does."""
    r = int(round(int(hex_color[1:3], 16) * (1 - amount) + 255 * amount))
    g = int(round(int(hex_color[3:5], 16) * (1 - amount) + 255 * amount))
    b = int(round(int(hex_color[5:7], 16) * (1 - amount) + 255 * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


TINT = {key: tint(value) for key, value in COLOR.items()}


def nice_axis(max_value, target_ticks: int = 4):
    """Return (axis_max, step) with a zero base and a readable tick count."""
    if not is_num(max_value) or float(max_value) <= 0:
        return 1.0, 0.25
    raw = float(max_value) / max(1, target_ticks)
    exponent = math.floor(math.log10(raw))
    base = raw / (10 ** exponent)
    for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0):
        if base <= multiplier + 1e-12:
            step = multiplier * (10 ** exponent)
            break
    else:  # pragma: no cover - the loop always breaks
        step = 10 ** (exponent + 1)
    axis_max = math.ceil((float(max_value) - 1e-9) / step) * step
    if axis_max <= 0:
        axis_max = step
    return axis_max, step


def fmt_tick(value, step: float) -> str:
    if abs(step - round(step)) < 1e-9 and step >= 1:
        return str(int(round(value)))
    decimals = 0 if step >= 1 else (1 if step >= 0.1 else 2)
    return fmt_num(value, decimals)


def short_sha(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return "—"
    return value.strip()[:12]


# --------------------------------------------------------------------------
# result loading


@dataclass
class Record:
    path: Path
    name: str
    raw: dict
    ts: datetime | None
    scenario: str
    rtt: float | None
    label: str
    metrics: dict
    inputs: dict


def rtt_key(value) -> str:
    if not is_num(value):
        return "unknown"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def rtt_text(value) -> str:
    if not is_num(value):
        return "RTT —"
    if float(value) == 0:
        return "RTT ≈ 0"
    return f"RTT {fmt_num(value, 2)} ms"


@dataclass
class Group:
    scenario: str
    records: dict
    newest: Record
    client: str
    server: str
    mixed: bool
    display: str
    color: str
    tint: str
    meta: str

    def rec(self, key: str):
        return self.records.get(key)


def load_results(results_dir: Path):
    """Return (records, scanned, ignored_names, invalid_names, state)."""
    if not results_dir.exists():
        return [], 0, [], [], "missing"
    if not results_dir.is_dir():
        return [], 0, [], [], "not-directory"

    records = []
    ignored = []
    invalid = []
    scanned = 0
    for path in sorted(results_dir.glob("*.json")):
        scanned += 1
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            invalid.append(path.name)
            continue
        if not isinstance(raw, dict):
            invalid.append(path.name)
            continue
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict) or not any(key in metrics for key in BENCH_KEYS):
            ignored.append(path.name)
            continue
        scenario = text_or_dash(raw.get("scenario"))
        if scenario == "—":
            scenario = "unknown"
        inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
        records.append(
            Record(
                path=path,
                name=path.name,
                raw=raw,
                ts=parse_ts(raw.get("ts")),
                scenario=scenario,
                rtt=float(raw["rtt_ms"]) if is_num(raw.get("rtt_ms")) else None,
                label=str(raw.get("label") or "").strip(),
                metrics=metrics,
                inputs=inputs,
            )
        )
    state = "empty" if not scanned else ("no-bench" if not records else "ok")
    return records, scanned, ignored, invalid, state


def split_runs(records):
    ordered = sorted(records, key=lambda record: (record.ts or MIN_DT, record.name))
    runs = []
    current = []
    for record in ordered:
        if current and record.ts and current[-1].ts:
            gap = (record.ts - current[-1].ts).total_seconds()
        else:
            gap = 0
        if current and gap > RUN_GAP_SECONDS:
            runs.append(current)
            current = []
        current.append(record)
    if current:
        runs.append(current)
    return runs


def select_latest(run):
    """Newest file per (scenario, RTT); deterministic on equal timestamps."""
    selected = {}
    for record in sorted(run, key=lambda item: (item.ts or MIN_DT, item.name)):
        selected[(record.scenario, rtt_key(record.rtt))] = record
    return list(selected.values())


# --------------------------------------------------------------------------
# engine / scenario labelling


def client_server(scenario: str):
    name = (scenario or "").lower()

    def server_from_suffix(default: str) -> str:
        if "ubisrv" in name or "unbihexsrv" in name:
            return "unbihex"
        if "xraysrv" in name:
            return "xray"
        if "singboxsrv" in name or "singbsrv" in name:
            return "singbox"
        return default

    if name.startswith("xraycl"):
        return "xray", server_from_suffix("unbihex")
    if name.startswith("ubicl") or name.startswith("unbihexcl"):
        return "unbihex", server_from_suffix("xray")
    if name.startswith("singcl") or name.startswith("singboxcl"):
        return "singbox", server_from_suffix("xray")
    if name.startswith("xray"):
        return "xray", "xray"
    if name.startswith("singbox"):
        return "singbox", "singbox"
    if name.startswith("unbihex"):
        return "unbihex", "unbihex"
    if "xraycl" in name and ("ubisrv" in name or "unbihexsrv" in name):
        return "xray", "unbihex"
    if ("ubicl" in name or "unbihexcl" in name) and "xraysrv" in name:
        return "unbihex", "xray"
    return "unknown", "unknown"


def input_key(kind: str, role: str) -> str:
    if kind == "unbihex":
        return "unbihex-cli" if role == "client" else "unbihex-server"
    return kind


def version_of(kind: str, role: str, inputs: dict) -> str:
    if kind not in ("xray", "singbox", "unbihex"):
        return ""
    entry = inputs.get(input_key(kind, role))
    if not isinstance(entry, dict):
        return ""
    version = entry.get("version")
    if not isinstance(version, str):
        return ""
    if kind == "xray":
        match = re.search(r"Xray\s+([0-9][^\s(]*)", version)
    elif kind == "singbox":
        match = re.search(r"sing-box\s+version\s+([0-9][^\s]*)", version)
    else:
        match = re.search(r"(\d+(?:\.\d+)+)", version)
    return match.group(1) if match else ""


def input_name(key: str) -> str:
    if key == "unbihex-server":
        return t("input.unbihex_server")
    if key == "unbihex-cli":
        return t("input.unbihex_cli")
    return INPUT_NAMES.get(key, key)


def self_suffix(scenario: str, records: dict, engine: str) -> str:
    name = (scenario or "").lower()
    if "xhttp" in name:
        return t("suffix.xhttp")
    if engine != "unbihex":
        return ""
    if "release" in name:
        return t("suffix.release")
    # Older Unbihex A/B result files named the released build "before" or
    # "stock"; both still collapse to the same label.
    labels = {
        (record.label or "").strip().lower()
        for record in records.values()
        if (record.label or "").strip()
    }
    if labels & {"pr", "pr #18", "after"}:
        return ""
    if labels & {"stock", "before"}:
        return t("suffix.stock")
    # A downloaded release binary carries no per-binary git block.  The CI
    # workflow names the scenario unbihex_release; this also covers a CI run
    # that keeps the unbihex_reality name.
    if labels <= {"", "ci"}:
        newest = max(records.values(), key=lambda item: (item.ts or MIN_DT, item.name))
        entry = newest.inputs.get("unbihex-cli")
        git = entry.get("git") if isinstance(entry, dict) else None
        if not isinstance(git, dict) or (
            git.get("dirty") is False
            and git.get("branch") in (None, "", "main", "master")
        ):
            return t("suffix.release")
    return ""


def engine_color(client: str, suffix: str) -> str:
    if client == "unbihex":
        return COLOR["unbihex_stock"]
    return COLOR.get(client, COLOR["unknown"])


def build_groups(selected):
    by_scenario = {}
    for record in selected:
        by_scenario.setdefault(record.scenario, {})[rtt_key(record.rtt)] = record

    groups = []
    for scenario, records in by_scenario.items():
        newest = max(
            records.values(),
            key=lambda item: (item.ts or MIN_DT, item.name),
        )
        client, server = client_server(scenario)
        mixed = client != server and client != "unknown"
        suffix = self_suffix(scenario, records, client) if client == server else ""
        client_name = ENGINE_NAMES.get(client, scenario)
        server_name = ENGINE_NAMES.get(server, scenario)
        client_version = version_of(client, "client", newest.inputs)
        server_version = version_of(server, "server", newest.inputs)

        if mixed:
            left = client_name + (f" {client_version}" if client_version else "")
            right = server_name + (f" {server_version}" if server_version else "")
            display = f"{left} client → {right} server"
        else:
            display = client_name
            if client_version:
                display += f" {client_version}"
            display += suffix

        labels = sorted({record.label for record in records.values() if record.label})
        meta = scenario + (f" · {' / '.join(labels)}" if labels else "")
        color = engine_color(client, suffix)
        groups.append(
            Group(
                scenario=scenario,
                records=records,
                newest=newest,
                client=client,
                server=server,
                mixed=mixed,
                display=display,
                color=color,
                tint=TINT["unbihex_stock"] if client == "unbihex" else TINT.get(
                    client, TINT["unknown"]
                ),
                meta=meta,
            )
        )

    groups.sort(
        key=lambda group: (
            ENGINE_ORDER.get(group.client, 9),
            1 if group.mixed else 0,
            group.scenario,
        )
    )
    return groups


# --------------------------------------------------------------------------
# metric access


def metric(record, key: str) -> dict:
    if record is None:
        return {}
    value = record.metrics.get(key)
    return value if isinstance(value, dict) else {}


def metric_value(record, key: str, field: str):
    value = metric(record, key).get(field)
    return float(value) if is_num(value) else None


def range_html(record, key: str) -> str:
    data = metric(record, key)
    low = data.get("min_mbps")
    high = data.get("max_mbps")
    if is_num(low) and is_num(high):
        return (
            f'<span class="range">{fmt_num(low, 1)}–{fmt_num(high, 1)}</span>'
        )
    return ""


def metric_cell(record, key: str) -> str:
    value = metric_value(record, key, "median_mbps")
    if value is None:
        return '<td class="num">—</td>'
    return (
        f'<td class="num">{fmt_num(value, 1)}{range_html(record, key)}</td>'
    )


# --------------------------------------------------------------------------
# validity, link profile, evidence and cost helpers


def validity_info(records) -> dict:
    records = [record for record in records if record is not None]
    total = len(records)
    known = [
        record.raw.get("valid")
        for record in records
        if isinstance(record.raw.get("valid"), bool)
    ]
    flagged = [record for record in records if record.raw.get("valid") is False]
    reasons = []
    for record in flagged:
        raw_reasons = record.raw.get("invalid_reasons")
        if isinstance(raw_reasons, list):
            for reason in raw_reasons:
                text = str(reason).strip()
                if text and text not in reasons:
                    reasons.append(text)
        elif isinstance(raw_reasons, str):
            text = raw_reasons.strip()
            if text and text not in reasons:
                reasons.append(text)
    if flagged:
        status = "invalid"
    elif total and len(known) == total:
        status = "valid"
    else:
        status = "unknown"
    return {
        "status": status,
        "reasons": reasons,
        "total": total,
        "known": len(known),
        "flagged": len(flagged),
    }


def validity_unknown_text(info: dict) -> str:
    if info["known"] == 0:
        return t("validity.unknown_none")
    return t(
        "validity.unknown_partial",
        known=info["known"],
        total=info["total"],
    )


def validity_badge(records, details: bool = False) -> str:
    info = validity_info(records)
    status = info["status"]
    if status == "valid":
        title = (
            t("validity.valid_one")
            if info["total"] == 1
            else t(
                "validity.valid_all",
                n=info["total"],
                files=p(info["total"], "result_files"),
            )
        )
        return (
            f'<span class="badge badge-valid" title="{esc(title)}">'
            f"{t('validity.badge.valid')}</span>"
        )
    if status == "unknown":
        title = validity_unknown_text(info)
        return (
            f'<span class="badge badge-unknown" title="{esc(title)}" '
            f'aria-label="{esc(t("validity.unchecked_aria"))}">'
            f"{t('validity.badge.unchecked')}</span>"
        )

    reasons = info["reasons"] or [t("validity.no_reason")]
    title = t("validity.invalid_title", reasons="; ".join(reasons))
    if details:
        count = ""
        if info["total"] > 1:
            count = (
                '<span class="validity-count">'
                + t(
                    "validity.count",
                    flagged=info["flagged"],
                    total=info["total"],
                )
                + "</span>"
            )
        items = "".join(f"<li>{esc(reason)}</li>" for reason in reasons)
        return (
            '<details class="validity">'
            f'<summary class="badge badge-invalid">{t("validity.badge.invalid")}'
            "</summary>"
            f'<div class="validity-pop"><strong>{t("validity.flagged")}</strong>'
            f"{count}<ul>{items}</ul></div></details>"
        )
    return (
        f'<span class="badge badge-invalid" title="{esc(title)}" '
        f'aria-label="{esc(title)}">{t("validity.badge.invalid")}</span>'
    )


def validity_notice(records) -> str:
    info = validity_info(records)
    if info["status"] != "invalid":
        return ""
    reasons = info["reasons"] or [t("validity.no_reason")]
    items = "".join(f"<li>{esc(reason)}</li>" for reason in reasons)
    count = ""
    if info["total"] > 1:
        count = (
            ' <span class="validity-count">'
            + t(
                "validity.count",
                flagged=info["flagged"],
                total=info["total"],
            )
            + "</span>"
        )
    return (
        '<div class="validity-notice">'
        f'<strong>{t("validity.notice_title")}{count}</strong>'
        f"<ul>{items}</ul>"
        f"<p>{t('validity.notice_body')}</p>"
        "</div>"
    )


def link_data(record: Record) -> dict:
    link = record.raw.get("link")
    return link if isinstance(link, dict) else {}


def profile_chip(record: Record) -> str:
    link = link_data(record)
    profile = link.get("profile")
    if isinstance(profile, str) and profile.strip():
        name = profile.strip()
        slug = name.lower() if name.lower() in ("wan", "loopback") else "other"
        return f'<span class="profile-chip profile-{slug}">{esc(name)}</span>'
    if link:
        return f'<span class="profile-chip profile-none">{t("profile.dash")}</span>'
    return (
        f'<span class="profile-chip profile-none">{t("profile.netem_only")}</span>'
    )


def link_delay_summary(links) -> str:
    one_ways = sorted({float(link["one_way_ms"]) for link in links if is_num(link.get("one_way_ms"))})
    jitters = sorted({float(link["jitter_ms"]) for link in links if is_num(link.get("jitter_ms"))})
    if not one_ways:
        return "—"
    text = t(
        "link.delay_one_way",
        values=" / ".join(fmt_num(value, 2) for value in one_ways),
    )
    if len(one_ways) > 1:
        text += t("link.per_rtt")
    if jitters:
        text += t(
            "link.jitter",
            values=" / ".join(fmt_num(value, 2) for value in jitters),
        )
    return text


def link_rate_text(link: dict) -> str:
    rate = link.get("rate_mbit")
    if not is_num(rate):
        return "—"
    if float(rate) == 0:
        return t("link.no_cap")
    return f"{fmt_num(rate, 1)} Mbit/s"


def link_loss_text(link: dict) -> str:
    loss = link.get("loss_pct")
    if not is_num(loss):
        return "—"
    return f"{fmt_num(loss, 3)}%"


def phase_sys(record: Record, key: str) -> dict:
    phase = metric(record, key)
    sys = phase.get("sys")
    if isinstance(sys, dict) and sys:
        return sys
    top = record.raw.get("sys")
    if isinstance(top, dict):
        if isinstance(top.get(key), dict):
            return top[key]
        if any(name in top for name in ("cpu", "ram", "engine_rss_kb")):
            return top
    return {}


def phase_evidence(record: Record, key: str) -> dict:
    phase = metric(record, key)
    evidence = phase.get("evidence")
    if isinstance(evidence, dict) and evidence:
        return evidence
    top = record.raw.get("evidence")
    if isinstance(top, dict):
        if isinstance(top.get(key), dict):
            return top[key]
        if any(name in top for name in ("qdisc", "sock")):
            return top
    return {}


def evidence_numbers(record: Record, key: str) -> dict:
    evidence = phase_evidence(record, key)
    qdisc = evidence.get("qdisc") if isinstance(evidence.get("qdisc"), dict) else {}
    sock = evidence.get("sock") if isinstance(evidence.get("sock"), dict) else {}
    total = (
        qdisc.get("qdisc_total")
        if isinstance(qdisc.get("qdisc_total"), dict)
        else {}
    )

    def qpick(name):
        if is_num(qdisc.get(name)):
            return float(qdisc[name])
        if name == "backlog" and is_num(qdisc.get("backlog_max")):
            return float(qdisc["backlog_max"])
        if is_num(total.get(name)):
            return float(total[name])
        return None

    def spick(name):
        return float(sock[name]) if is_num(sock.get(name)) else None

    dropped = qpick("dropped")
    packets = qpick("packets")
    rate = qdisc.get("drop_rate_pct") if is_num(qdisc.get("drop_rate_pct")) else None
    if rate is None:
        total_rate = total.get("drop_rate_pct")
        if is_num(total_rate):
            rate = float(total_rate)
    if rate is None and dropped is not None and packets and packets > 0:
        rate = dropped / packets * 100.0

    queue_limit = None
    backlog_packets = None
    qdisc_raw = qdisc.get("qdisc_raw")
    if isinstance(qdisc_raw, str):
        match = re.search(r"\blimit\s+(\d+)", qdisc_raw)
        if match:
            queue_limit = float(match.group(1))
        packet_backlogs = [
            float(value)
            for value in re.findall(r"backlog\s+\d+b\s+(\d+)p", qdisc_raw)
        ]
        if packet_backlogs:
            backlog_packets = max(packet_backlogs)

    return {
        "dropped": dropped,
        "packets": packets,
        "drop_rate_pct": float(rate) if rate is not None else None,
        "overlimits": qpick("overlimits"),
        "requeues": qpick("requeues"),
        "backlog": qpick("backlog"),
        "backlog_packets": backlog_packets,
        "queue_limit": queue_limit,
        "retrans": spick("retrans"),
        "sockets": spick("sockets"),
        "has": bool(evidence),
        "qdisc_raw": qdisc_raw,
        "sock_raw": sock.get("sock_raw"),
    }


def evidence_assessment(record: Record, key: str) -> dict:
    numbers = evidence_numbers(record, key)
    link = link_data(record)
    loss = float(link["loss_pct"]) if is_num(link.get("loss_pct")) else None
    threshold = max(
        EVIDENCE_LOSS_MULTIPLIER * (loss or 0.0),
        EVIDENCE_MIN_DROP_THRESHOLD,
    )
    reasons = []
    overlimits = numbers["overlimits"]
    if overlimits is not None and overlimits > 0:
        reasons.append(t("evidence.reason.overlimits", n=fmt_int(overlimits)))
    rate = numbers["drop_rate_pct"]
    if rate is not None and rate > threshold + 1e-9:
        suffix = t("evidence.reason.drop_unknown") if loss is None else ""
        reasons.append(
            t(
                "evidence.reason.drop",
                rate=fmt_num(rate, 3),
                threshold=fmt_num(threshold, 3),
                suffix=suffix,
            )
        )
    backlog = (
        numbers["backlog_packets"]
        if numbers["backlog_packets"] is not None
        else numbers["backlog"]
    )
    limit = numbers["queue_limit"]
    if (
        backlog is not None
        and limit is not None
        and limit > 0
        and backlog >= EVIDENCE_BACKLOG_FRACTION * limit
    ):
        unit = "pkt " if numbers["backlog_packets"] is not None else ""
        reasons.append(
            t(
                "evidence.reason.backlog",
                n=fmt_int(backlog),
                unit=unit,
                limit=fmt_int(limit),
            )
        )
    within_loss = (
        not reasons
        and (numbers["dropped"] or 0) > 0
        and rate is not None
        and loss is not None
        and rate <= loss + 1e-9
    )
    return {
        **numbers,
        "bottleneck": bool(reasons),
        "reasons": reasons,
        "threshold": threshold,
        "configured_loss": loss,
        "loss_unknown": loss is None,
        "within_loss": within_loss,
        "phase_key": key,
        "phase_label": phase_label(key),
        "rtt": record.rtt,
    }


def group_evidence(group: Group) -> dict:
    assessments = []
    has = False
    for record in group.records.values():
        for key in PHASES:
            evidence = phase_evidence(record, key)
            if evidence:
                has = True
            if not evidence:
                continue
            assessments.append(evidence_assessment(record, key))
    if not assessments:
        return {
            "has": has,
            "dropped": None,
            "drop_rate_pct": None,
            "overlimits": None,
            "backlog": None,
            "retrans": None,
            "sockets": None,
            "bottleneck": False,
            "reasons": [],
            "configured_loss": None,
            "within_loss": False,
            "loss_unknown": False,
        }

    bottleneck_assessments = [item for item in assessments if item["bottleneck"]]
    candidates = bottleneck_assessments or assessments
    display = max(
        candidates,
        key=lambda item: (
            item["dropped"] or 0,
            item["drop_rate_pct"] if item["drop_rate_pct"] is not None else -1,
            item["overlimits"] or 0,
            item["backlog"] or 0,
        ),
    )

    def max_value(name):
        values = [item[name] for item in assessments if item[name] is not None]
        return max(values) if values else None

    reasons = []
    for item in assessments:
        if not item["bottleneck"]:
            continue
        for reason in item["reasons"]:
            text = f"{item['phase_label']} {rtt_text(item['rtt'])}: {reason}"
            if text not in reasons:
                reasons.append(text)

    return {
        "has": True,
        "dropped": display["dropped"],
        "drop_rate_pct": display["drop_rate_pct"],
        "overlimits": max_value("overlimits"),
        "backlog": max_value("backlog"),
        "retrans": max_value("retrans"),
        "sockets": max_value("sockets"),
        "bottleneck": bool(reasons),
        "reasons": reasons,
        "configured_loss": display["configured_loss"],
        "within_loss": display["within_loss"],
        "loss_unknown": any(item["loss_unknown"] for item in assessments),
    }


def evidence_line(group: Group, compact: bool = False) -> str:
    evidence = group_evidence(group)
    if evidence["has"]:
        drops = fmt_int(evidence["dropped"])
        rate = (
            f"{fmt_num(evidence['drop_rate_pct'], 3)}%"
            if evidence["drop_rate_pct"] is not None
            else "—%"
        )
        overlimits = fmt_int(evidence["overlimits"])
        retrans = fmt_int(evidence["retrans"])
    else:
        drops = "—"
        rate = "—"
        overlimits = "—"
        retrans = "—"

    neutral = ""
    if (
        evidence["has"]
        and not evidence["bottleneck"]
        and evidence["within_loss"]
        and (evidence["dropped"] or 0) > 0
    ):
        neutral = (
            f'<span class="evidence-note">'
            f"{t('evidence.loss_configured', rate=fmt_num(evidence['configured_loss'], 3))}"
            "</span>"
        )

    warning = ""
    if evidence["bottleneck"]:
        label = (
            t("evidence.netem_short") if compact else t("evidence.netem_long")
        )
        title = t("evidence.netem_long") + ": " + "; ".join(evidence["reasons"])
        warning = (
            f'<span class="netem-warning" title="{esc(title)}">'
            f"{label}</span>"
        )
    if evidence["has"]:
        evidence_text = t(
            "evidence.line",
            drops=drops,
            rate=rate,
            overlimits=overlimits,
            retrans=retrans,
        )
    else:
        evidence_text = t("evidence.line_empty")
    return (
        f'<span class="group-evidence" title="{esc(t("evidence.tooltip"))}">'
        f"{evidence_text}</span>{neutral}{warning}"
    )


def phase_runs(record: Record, key: str):
    phase = metric(record, key)
    value = phase.get("runs")
    if is_num(value):
        return float(value)
    sys = phase_sys(record, key)
    value = sys.get("runs")
    return float(value) if is_num(value) else None


def phase_nproc(record: Record, cpu: dict):
    for value in (
        cpu.get("nproc"),
        (record.raw.get("machine") or {}).get("nproc")
        if isinstance(record.raw.get("machine"), dict)
        else None,
        record.raw.get("nproc"),
    ):
        if is_num(value):
            return float(value)
    return None


def phase_wall_seconds(record: Record, key: str):
    """Derive phase wall time only from recorded transfer/rate data."""
    if key in ("down1", "down8", "up1"):
        phase = metric(record, key)
        cpu_summary = metric(record, key + "_cpu")
        mib = None
        for source in (cpu_summary, phase):
            for field in ("mib", "mib_total"):
                if is_num(source.get(field)):
                    mib = float(source[field])
                    break
            if mib is None:
                for field in ("bytes", "bytes_total"):
                    if is_num(source.get(field)):
                        mib = float(source[field]) / (1024 * 1024)
                        break
            if mib is not None:
                break
        median = metric_value(record, key, "median_mbps")
        if mib is not None and median is not None and median > 0:
            return mib / median
    if key == "conns":
        count = metric_value(record, key, "count")
        rate = metric_value(record, key, "conns_per_sec")
        if count is not None and rate is not None and rate > 0:
            return count / rate
    return None


def ram_summary(records) -> dict:
    total = None
    available = None
    source = "sys"
    for record in records:
        for key in PHASES:
            sys = phase_sys(record, key)
            ram = sys.get("ram") if isinstance(sys.get("ram"), dict) else {}
            if not ram:
                continue
            if is_num(ram.get("mem_total_kb")):
                value = float(ram["mem_total_kb"])
                total = value if total is None else max(total, value)
            candidates = []
            if is_num(ram.get("available_min_kb")):
                candidates.append(float(ram["available_min_kb"]))
            for field in ("mem_available_before_kb", "mem_available_after_kb"):
                if is_num(ram.get(field)):
                    candidates.append(float(ram[field]))
            if candidates:
                value = min(candidates)
                available = value if available is None else min(available, value)
    if total is None and available is None:
        machine = record_machine(records)
        if machine:
            source = "machine"
            if is_num(machine.get("mem_total_kb")):
                total = float(machine["mem_total_kb"])
            if is_num(machine.get("mem_available_kb_at_start")):
                available = float(machine["mem_available_kb_at_start"])
    return {"total": total, "available": available, "source": source}


def record_machine(records) -> dict:
    for record in records:
        machine = record.raw.get("machine")
        if isinstance(machine, dict):
            return machine
    return {}


def record_nproc(record: Record):
    machine = record.raw.get("machine") if isinstance(record.raw.get("machine"), dict) else {}
    if is_num(machine.get("nproc")):
        return float(machine["nproc"])
    if is_num(record.raw.get("nproc")):
        return float(record.raw["nproc"])
    return None


def rss_summary(records) -> dict:
    peaks = {"server": None, "client": None}
    for record in records:
        for key in PHASES:
            sys = phase_sys(record, key)
            rss = (
                sys.get("engine_rss_kb")
                if isinstance(sys.get("engine_rss_kb"), dict)
                else {}
            )
            for role in ("server", "client"):
                entry = rss.get(role) if isinstance(rss.get(role), dict) else {}
                values = [
                    float(entry[field])
                    for field in ("rss_max_kb", "hwm_kb", "rss_kb")
                    if is_num(entry.get(field))
                ]
                if values:
                    peak = max(values)
                    current = peaks[role]
                    peaks[role] = peak if current is None else max(current, peak)
    return peaks


def any_cpu_rows(group: Group) -> bool:
    for record in group.records.values():
        for key in PHASES:
            cpu = phase_sys(record, key).get("cpu")
            if isinstance(cpu, dict) and cpu:
                return True
    return False


def any_evidence_rows(group: Group) -> bool:
    for record in group.records.values():
        for key in PHASES:
            if phase_evidence(record, key):
                return True
    return False


# --------------------------------------------------------------------------
# chart rendering (horizontal HTML/CSS bars, 0-based axes)


def simple_engine_color(kind: str) -> str:
    if kind == "unbihex":
        return COLOR["unbihex_stock"]
    return COLOR.get(kind, COLOR["unknown"])


def group_swatch(group: Group) -> str:
    if group.mixed:
        server_color = simple_engine_color(group.server)
        style = (
            f"background:linear-gradient(135deg,{group.color} 0 49%,"
            f"{server_color} 51% 100%)"
        )
    else:
        style = f"background:{group.color}"
    return f'<span class="swatch" style="{style}" aria-hidden="true"></span>'


def group_is_invalid(group: Group) -> bool:
    return validity_info(group.records.values())["status"] == "invalid"


def group_head(group: Group) -> str:
    tag = (
        f'<span class="mini-tag">{t("run.mixed_pair")}</span>' if group.mixed else ""
    )
    return (
        '<div class="group-head">'
        f'<span class="group-title">{group_swatch(group)}'
        f'<span class="group-name">{esc(group.display)}</span></span>'
        f"{tag}"
        f"{validity_badge(group.records.values())}"
        f'<span class="group-meta">{esc(group.meta)}</span>'
        f"{evidence_line(group, compact=True)}"
        "</div>"
    )


def axis_html(axis_max: float, step: float, unit: str) -> str:
    ticks = max(1, int(round(axis_max / step)))
    pieces = []
    for index in range(ticks + 1):
        value = index * step
        fraction = index / ticks * 100
        klass = "tick"
        if index == 0:
            klass += " first"
        elif index == ticks:
            klass += " last"
        pieces.append(
            f'<span class="{klass}" style="left:{fraction:.4f}%">'
            f"{esc(fmt_tick(value, step))}</span>"
        )
    return (
        '<div class="axis">'
        f'<span class="axis-unit">{esc(unit)}</span>'
        f'<div class="axis-plot">{"".join(pieces)}</div>'
        "<span></span>"
        "</div>"
    )


def bar_row(
    label: str,
    value,
    maximum: float,
    color: str,
    tint_color: str,
    index: int,
    *,
    tinted: bool = False,
    value_html: str | None = None,
    p95=None,
) -> str:
    if value is None:
        fill = ""
        display_value = value_html if value_html is not None else "—"
    else:
        percent = clamp_pct(value, maximum)
        classes = "fill"
        if tinted:
            classes += " tint"
        if percent == 0:
            classes += " is-zero"
        style = (
            f"--bar:{color};--v:{percent:.4f}%;--i:{index}"
        )
        if tinted:
            style += f";--bar-tint:{tint_color}"
        fill = f'<span class="{classes}" style="{style}"></span>'
        display_value = (
            value_html if value_html is not None else esc(fmt_num(value, 1))
        )

    marker = ""
    if p95 is not None:
        marker = (
            f'<span class="p95" style="--p:{clamp_pct(p95, maximum):.4f}%;'
            f'--bar:{color}"></span>'
        )
    return (
        '<div class="bar-row">'
        f'<span class="bar-label">{esc(label)}</span>'
        f'<div class="track">{fill}{marker}</div>'
        f'<span class="bar-value">{display_value}</span>'
        "</div>"
    )


def chart_shell(axis_max, step, unit, groups_html) -> str:
    ticks = max(1, int(round(axis_max / step)))
    return (
        f'<div class="chart" style="--div:{ticks}" aria-hidden="true">'
        f"{axis_html(axis_max, step, unit)}"
        f'<div class="bar-groups">{groups_html}</div>'
        "</div>"
    )


def throughput_chart(groups) -> str:
    series = (
        ("down1", "↓1"),
        ("down8", "↓8"),
        ("up1", "↑1"),
    )
    values = []
    for group in groups:
        record = group.rec("0")
        for key, _ in series:
            value = metric_value(record, key, "median_mbps")
            if value is not None:
                values.append(value)
    axis_max, step = nice_axis(max(values) if values else 0)

    blocks = []
    row_index = 0
    for group in groups:
        record = group.rec("0")
        rows = []
        for key, label in series:
            value = metric_value(record, key, "median_mbps")
            rows.append(
                bar_row(
                    label,
                    value,
                    axis_max,
                    group.color,
                    group.tint,
                    row_index,
                )
            )
            row_index += 1
        extra = " is-invalid" if group_is_invalid(group) else ""
        blocks.append(
            f'<div class="bar-group{extra}">{group_head(group)}{"".join(rows)}</div>'
        )
    return chart_shell(axis_max, step, "MB/s", "".join(blocks))


def rtt_chart(groups) -> str:
    values = []
    for group in groups:
        for key in ("0", "50"):
            value = metric_value(group.rec(key), "down1", "median_mbps")
            if value is not None:
                values.append(value)
    axis_max, step = nice_axis(max(values) if values else 0)

    blocks = []
    row_index = 0
    for group in groups:
        rows = []
        for key, label, tinted in (("0", "RTT ≈ 0", False), ("50", "RTT 50 ms", True)):
            value = metric_value(group.rec(key), "down1", "median_mbps")
            rows.append(
                bar_row(
                    label,
                    value,
                    axis_max,
                    group.color,
                    group.tint,
                    row_index,
                    tinted=tinted,
                )
            )
            row_index += 1
        extra = " is-invalid" if group_is_invalid(group) else ""
        blocks.append(
            f'<div class="bar-group{extra}">{group_head(group)}{"".join(rows)}</div>'
        )
    return chart_shell(axis_max, step, "MB/s", "".join(blocks))


def ttfb_chart(groups) -> str:
    values = []
    for group in groups:
        record = group.rec("0")
        for field in ("median_ms", "p95_ms"):
            value = metric_value(record, "ttfb", field)
            if value is not None:
                values.append(value)
    axis_max, step = nice_axis(max(values) if values else 0)

    blocks = []
    for index, group in enumerate(groups):
        record = group.rec("0")
        median = metric_value(record, "ttfb", "median_ms")
        p95 = metric_value(record, "ttfb", "p95_ms")
        if median is None and p95 is None:
            value_html = "—"
        else:
            median_text = esc(fmt_num(median, 2)) if median is not None else "—"
            p95_text = esc(fmt_num(p95, 2)) if p95 is not None else "—"
            value_html = f"{median_text}<small>p95 {p95_text}</small>"
        row = bar_row(
            "TTFB",
            median,
            axis_max,
            group.color,
            group.tint,
            index,
            value_html=value_html,
            p95=p95,
        )
        extra = " is-invalid" if group_is_invalid(group) else ""
        blocks.append(
            f'<div class="bar-group{extra}">{group_head(group)}{row}</div>'
        )
    return chart_shell(axis_max, step, "ms", "".join(blocks))


def conns_chart(groups) -> str:
    values = [
        metric_value(group.rec("0"), "conns", "conns_per_sec")
        for group in groups
    ]
    values = [value for value in values if value is not None]
    axis_max, step = nice_axis(max(values) if values else 0)

    blocks = []
    for index, group in enumerate(groups):
        value = metric_value(group.rec("0"), "conns", "conns_per_sec")
        row = bar_row(
            "conn/s",
            value,
            axis_max,
            group.color,
            group.tint,
            index,
        )
        extra = " is-invalid" if group_is_invalid(group) else ""
        blocks.append(
            f'<div class="bar-group{extra}">{group_head(group)}{row}</div>'
        )
    return chart_shell(axis_max, step, "conn/s", "".join(blocks))


# --------------------------------------------------------------------------
# tables


def group_row_class(group: Group) -> str:
    return ' class="is-invalid"' if group_is_invalid(group) else ""


def row_head(group: Group) -> str:
    return (
        f'<span class="row-name">{esc(group.display)}</span>'
        f'<span class="row-meta">{esc(group.meta)}</span>'
        '<span class="row-flags">'
        f"{validity_badge(group.records.values())}"
        f"{evidence_line(group, compact=True)}"
        "</span>"
    )


def table_throughput(groups) -> str:
    rows = []
    for group in groups:
        record = group.rec("0")
        rows.append(
            f"<tr{group_row_class(group)}>"
            f'<th scope="row">{row_head(group)}</th>'
            f"{metric_cell(record, 'down1')}"
            f"{metric_cell(record, 'down8')}"
            f"{metric_cell(record, 'up1')}"
            "</tr>"
        )
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("table.throughput.aria"))}">'
        '<table class="t-throughput">'
        f"<caption>{t('table.throughput.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("table.engine")}</th>'
        '<th scope="col">↓1 MB/s</th>'
        '<th scope="col">↓8 MB/s</th>'
        '<th scope="col">↑1 MB/s</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def table_rtt(groups) -> str:
    rows = []
    for group in groups:
        rows.append(
            f"<tr{group_row_class(group)}>"
            f'<th scope="row">{row_head(group)}</th>'
            f"{metric_cell(group.rec('0'), 'down1')}"
            f"{metric_cell(group.rec('50'), 'down1')}"
            "</tr>"
        )
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("table.rtt.aria"))}">'
        '<table class="t-rtt">'
        f"<caption>{t('table.rtt.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("table.engine")}</th>'
        '<th scope="col">↓1, RTT ≈ 0</th>'
        '<th scope="col">↓1, RTT 50 ms</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def plain_num_cell(record, key: str, field: str, digits: int) -> str:
    value = metric_value(record, key, field)
    if value is None:
        return '<td class="num">—</td>'
    return f'<td class="num">{fmt_num(value, digits)}</td>'


def integer_cell(record, key: str, field: str) -> str:
    value = metric(record, key).get(field)
    if not is_num(value):
        return '<td class="num">—</td>'
    return f'<td class="num">{fmt_int(value)}</td>'


def table_ttfb(groups) -> str:
    rows = []
    for group in groups:
        record = group.rec("0")
        rows.append(
            f"<tr{group_row_class(group)}>"
            f'<th scope="row">{row_head(group)}</th>'
            f"{plain_num_cell(record, 'ttfb', 'median_ms', 2)}"
            f"{plain_num_cell(record, 'ttfb', 'p95_ms', 2)}"
            f"{integer_cell(record, 'ttfb', 'runs')}"
            f"{integer_cell(record, 'ttfb', 'errors')}"
            "</tr>"
        )
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("table.ttfb.aria"))}">'
        '<table class="t-ttfb">'
        f"<caption>{t('table.ttfb.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("table.engine")}</th>'
        f'<th scope="col">{t("table.ttfb.median")}</th>'
        f'<th scope="col">{t("table.ttfb.p95")}</th>'
        f'<th scope="col">{t("table.connections")}</th>'
        f'<th scope="col">{t("table.errors")}</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def table_conns(groups) -> str:
    rows = []
    for group in groups:
        record = group.rec("0")
        rows.append(
            f"<tr{group_row_class(group)}>"
            f'<th scope="row">{row_head(group)}</th>'
            f"{plain_num_cell(record, 'conns', 'conns_per_sec', 1)}"
            f"{integer_cell(record, 'conns', 'count')}"
            f"{integer_cell(record, 'conns', 'par')}"
            f"{integer_cell(record, 'conns', 'errors')}"
            "</tr>"
        )
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("table.conns.aria"))}">'
        '<table class="t-conns">'
        f"<caption>{t('table.conns.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("table.engine")}</th>'
        f'<th scope="col">{t("table.conns.rate")}</th>'
        f'<th scope="col">{t("table.connections")}</th>'
        f'<th scope="col">{t("table.conns.inflight")}</th>'
        f'<th scope="col">{t("table.errors")}</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def chart_block(
    chart_id: str,
    title: str,
    subtitle: str,
    chart: str,
    foot: str,
    table: str,
    legend: str = "",
    extra_class: str = "",
) -> str:
    return (
        f'<article class="chart-block {extra_class}">'
        '<div class="chart-head">'
        f'<div><h3 id="{chart_id}">{title}</h3><p>{subtitle}</p></div>'
        f"{legend}"
        "</div>"
        f"{chart}"
        f'<p class="chart-foot">{foot}</p>'
        f"{table}"
        "</article>"
    )


# --------------------------------------------------------------------------
# latest-run section


def kv_item(label: str, value: str, mono: bool = False) -> str:
    klass = ' class="mono"' if mono else ""
    return f"<div><dt>{esc(label)}</dt><dd{klass}>{value}</dd></div>"


def card(title: str, body: str, extra_class: str = "") -> str:
    return (
        f'<article class="card {extra_class}">'
        f"<h3>{esc(title)}</h3>{body}</article>"
    )


def hardware_card(record: Record) -> str:
    raw = record.raw
    machine = raw.get("machine") if isinstance(raw.get("machine"), dict) else {}
    cpu_model = machine.get("cpu_model") or raw.get("cpu")
    nproc = machine.get("nproc") if is_num(machine.get("nproc")) else raw.get("nproc")
    rows = [
        kv_item(t("kv.cpu"), esc(text_or_dash(cpu_model))),
        kv_item(
            t("kv.nproc"),
            esc(f"{fmt_int(nproc)} vCPU") if is_num(nproc) else "—",
            mono=True,
        ),
        kv_item(t("kv.kernel"), esc(text_or_dash(raw.get("kernel"))), mono=True),
    ]
    mem_total = machine.get("mem_total_kb")
    mem_available = machine.get("mem_available_kb_at_start")
    if is_num(mem_total) or is_num(mem_available):
        memory = (
            t(
                "mem.free_total",
                free=fmt_kb(mem_available),
                total=fmt_kb(mem_total),
            )
            if is_num(mem_available) and is_num(mem_total)
            else fmt_kb(mem_available) if is_num(mem_available) else fmt_kb(mem_total)
        )
        title = ""
        if is_num(mem_total) or is_num(mem_available):
            title = (
                f' title="{esc(t("mem.free_title", free=fmt_int(mem_available), total=fmt_int(mem_total)))}"'
            )
        rows.append(
            kv_item(t("kv.memory"), f'<span{title}>{esc(memory)}</span>', mono=True)
        )
    sysctl = raw.get("sysctl") if isinstance(raw.get("sysctl"), dict) else {}
    for key in ("net.ipv4.tcp_rmem", "net.core.rmem_max", "net.core.wmem_max"):
        if key in sysctl:
            value = " ".join(str(sysctl[key]).split())
            rows.append(kv_item(key, esc(value), mono=True))
    return card(t("card.hardware"), f'<dl class="kv">{"".join(rows)}</dl>')


def link_card(record: Record, records=None) -> str:
    link = link_data(record)
    link_records = [
        link_data(item) for item in (records or [record]) if link_data(item)
    ]
    netem = record.raw.get("netem") if isinstance(record.raw.get("netem"), dict) else {}
    ports = netem.get("ports")
    if isinstance(ports, list) and ports:
        ports_text = ", ".join(str(port) for port in ports)
    else:
        ports_text = "—"
    rows = [kv_item(t("link.kv.profile"), profile_chip(record), mono=True)]
    note = ""
    if link:
        rows.extend(
            [
                kv_item(t("link.kv.delay"), esc(link_delay_summary(link_records)), mono=True),
                kv_item(t("link.kv.loss"), esc(link_loss_text(link)), mono=True),
                kv_item(t("link.kv.rate"), esc(link_rate_text(link)), mono=True),
                kv_item(t("link.kv.mtu"), esc(fmt_int(link.get("mtu"))), mono=True),
                kv_item(t("link.kv.gso"), esc(text_or_dash(link.get("gso"))), mono=True),
                kv_item(t("link.kv.ports"), esc(ports_text), mono=True),
                kv_item(t("link.kv.queue"), esc(text_or_dash(netem.get("limit"))), mono=True),
            ]
        )
    else:
        one_way = netem.get("one_way_ms")
        if not is_num(one_way):
            delay = "—"
        elif float(one_way) == 0:
            delay = t("link.delay_off")
        else:
            delay = t(
                "link.delay_each_way",
                one=fmt_num(one_way, 2),
                rtt=fmt_num(float(one_way) * 2, 2),
            )
        rows.extend(
            [
                kv_item(t("link.kv.delay"), esc(delay), mono=True),
                kv_item(t("link.kv.queue"), esc(text_or_dash(netem.get("limit"))), mono=True),
                kv_item(t("link.kv.ports"), esc(ports_text), mono=True),
            ]
        )
        note = f'<p class="card-note">{t("link.legacy_note")}</p>'
    return card(
        t("link.card_title"),
        f'<dl class="kv">{"".join(rows)}</dl>{note}',
    )


def source_card(record: Record, run_start, run_end) -> str:
    git = record.raw.get("git") if isinstance(record.raw.get("git"), dict) else {}
    if not git:
        input_keys = [key for key in INPUT_ORDER if key in record.inputs]
        input_keys += sorted(key for key in record.inputs if key not in INPUT_ORDER)
        for key in input_keys:
            entry = record.inputs.get(key)
            candidate = entry.get("git") if isinstance(entry, dict) else None
            if isinstance(candidate, dict) and candidate.get("commit"):
                git = candidate
                break
    commit = git.get("commit")
    branch = git.get("branch")
    dirty = git.get("dirty")
    if isinstance(commit, str) and commit:
        commit_html = f'<code title="{esc(commit)}">{esc(commit[:12])}</code>'
    else:
        commit_html = "—"
    details = []
    if isinstance(branch, str) and branch:
        details.append(esc(branch))
    if dirty is True:
        details.append(t("git.dirty"))
    elif dirty is False:
        details.append(t("git.clean"))
    rows = [
        kv_item(t("git.commit"), commit_html, mono=True),
        kv_item(t("git.branch"), " · ".join(details) if details else "—", mono=True),
        kv_item(
            t("git.run_window"),
            f"{esc(fmt_dt(run_start))}<br>{esc(fmt_dt(run_end))}",
            mono=True,
        ),
        kv_item(t("git.newest"), esc(fmt_dt(record.ts)), mono=True),
    ]
    return card(t("card.source"), f'<dl class="kv">{"".join(rows)}</dl>')


def binaries_card(record: Record) -> str:
    inputs = record.inputs
    keys = [key for key in INPUT_ORDER if key in inputs]
    keys += sorted(key for key in inputs if key not in INPUT_ORDER)
    if not keys:
        return card(
            t("card.binaries"),
            f'<p class="card-empty">{t("binaries.empty")}</p>',
        )
    rows = []
    for key in keys:
        entry = inputs.get(key) if isinstance(inputs.get(key), dict) else {}
        name = input_name(key)
        version = text_or_dash(entry.get("version"))
        sha = entry.get("sha256")
        if isinstance(sha, str) and sha.strip():
            sha_html = (
                f'<code title="{esc(sha.strip())}">{esc(sha.strip()[:12])}</code>'
            )
        else:
            sha_html = "—"
        rows.append(
            "<tr>"
            f'<th scope="row">{esc(name)}</th>'
            f'<td class="bin-version" data-label="{esc(t("binaries.label.version"))}">'
            f"{esc(version)}</td>"
            f'<td class="bin-sha" data-label="{esc(t("binaries.label.sha"))}">'
            f"{sha_html}</td>"
            "</tr>"
        )
    return card(
        t("card.binaries"),
        '<div class="table-wrap table-plain" role="region" tabindex="0" '
        f'aria-label="{esc(t("binaries.aria"))}">'
        '<table class="t-binaries">'
        f'<caption class="sr-only">{t("binaries.caption")}</caption>'
        "<thead><tr>"
        f'<th scope="col">{t("binaries.col.binary")}</th>'
        f'<th scope="col">{t("binaries.col.version")}</th>'
        f'<th scope="col">{t("binaries.col.sha")}</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>",
        "card-wide",
    )


def run_callout(record: Record | None) -> str:
    nproc = record_nproc(record) if record else None
    if is_num(nproc) and float(nproc) != 4:
        first = t("run.callout.first", n=fmt_int(nproc))
    else:
        first = t("run.callout.first_default")
    return (
        f'<aside class="callout" aria-label="{esc(t("run.callout.aria"))}">'
        '<span class="callout-mark" aria-hidden="true">!</span>'
        "<div>"
        f"<strong>{t('run.callout.title')}</strong>"
        f"<p>{esc(first)} {t('run.callout.body')}</p>"
        "</div></aside>"
    )


def run_sources_details(run, selected, ignored, invalid, scanned, total_bench, state) -> str:
    items = []
    for record in sorted(
        selected, key=lambda item: (item.scenario, item.rtt if item.rtt is not None else 1e9)
    ):
        rtt = f"RTT {fmt_num(record.rtt, 2)} ms" if record.rtt is not None else "RTT —"
        items.append(
            f'<li><span class="source-scenario">{esc(record.scenario)}</span> '
            f"<span>{esc(rtt)}</span> · {esc(record.name)}</li>"
        )
    notes = [
        t("sources.scanned", n=scanned, files=p(scanned, "files")),
        t(
            "sources.charted",
            n=len(selected),
            result_files=p(len(selected), "result_files"),
            m=len(run),
            throughput_files=p(len(run), "throughput_files"),
        ),
    ]
    if len(run) > len(selected):
        superseded = len(run) - len(selected)
        notes.append(
            t("sources.superseded", n=superseded, files=p(superseded, "files"))
        )
    older = total_bench - len(run)
    if older > 0:
        notes.append(
            t("sources.older", n=older, result_files=p(older, "result_files"))
        )
    if ignored:
        notes.append(
            t("sources.ignored", n=len(ignored), files=p(len(ignored), "files"))
        )
    if invalid:
        notes.append(
            t(
                "sources.invalid",
                n=len(invalid),
                files=p(len(invalid), "files"),
                names=", ".join(esc(name) for name in invalid),
            )
        )
    return (
        '<details class="sources">'
        f"<summary>{t('sources.summary')}</summary>"
        f'<ul class="source-list">{"".join(items)}</ul>'
        f'<p class="details-note">{" ".join(notes)}</p>'
        f'<p class="details-note">{t("sources.raw", results_url=CI_RESULTS_URL, actions_url=ACTIONS_URL)}</p>'
        "</details>"
    )


def render_run_section(records, selected, groups, run, ignored, invalid, scanned, state):
    parts = []
    if selected:
        newest = max(selected, key=lambda item: (item.ts or MIN_DT, item.name))
        linked = [record for record in selected if link_data(record)]
        link_record = (
            max(linked, key=lambda item: (item.ts or MIN_DT, item.name))
            if linked
            else newest
        )
        run_start = min((record.ts for record in run if record.ts), default=None)
        run_end = max((record.ts for record in run if record.ts), default=None)
        parts.append(
            '<div class="run-strip">'
            f"{profile_chip(link_record)}"
            f"{validity_badge(selected, details=True)}"
            f'<span class="chip"><strong>{len(groups)}</strong> '
            f"{t('run.chip.scenarios', n=len(groups), word=p(len(groups), 'scenarios'))}</span>"
            f'<span class="chip"><strong>{len(selected)}</strong> '
            f"{t('run.chip.results', n=len(selected), word=p(len(selected), 'result_files'))}</span>"
            f'<span class="chip"><strong>{esc(fmt_date(newest.ts))}</strong> '
            f"{t('run.chip.date', date='')}</span>"
            f'<span class="chip"><strong>{esc(fmt_int(record_nproc(newest)))}</strong> vCPU</span>'
            "</div>"
        )
        parts.append(validity_notice(selected))
        run_validity = validity_info(selected)
        if run_validity["status"] == "unknown":
            parts.append(
                '<p class="details-note">'
                f"{esc(validity_unknown_text(run_validity))} "
                f"{t('run.unknown_suffix')}</p>"
            )
        missing_link = sum(1 for record in selected if not link_data(record))
        if linked and missing_link:
            parts.append(
                '<p class="details-note">'
                + t(
                    "run.missing_link",
                    n=missing_link,
                    files=p(missing_link, "result_files"),
                    verb="has" if missing_link == 1 else "have",
                )
                + "</p>"
            )
        profiles = sorted(
            {
                str(link_data(record).get("profile")).strip().lower()
                for record in selected
                if link_data(record).get("profile")
            }
        )
        if len(profiles) > 1:
            parts.append(
                '<p class="details-note">'
                + t(
                    "run.multiple_profiles",
                    profiles=", ".join(esc(profile) for profile in profiles),
                )
                + "</p>"
            )
        parts.append(run_callout(newest))
        parts.append(
            '<div class="meta-grid">'
            + hardware_card(newest)
            + link_card(link_record, selected)
            + source_card(newest, run_start, run_end)
            + "</div>"
        )
        parts.append(binaries_card(newest))
        parts.append(
            run_sources_details(
                run, selected, ignored, invalid, scanned, len(records), state
            )
        )
    else:
        if state == "missing":
            message = t("empty.missing")
        elif state == "empty":
            message = t("empty.empty_dir")
        elif state == "not-directory":
            message = t("empty.not_directory")
        else:
            message = t(
                "empty.no_bench",
                n=scanned,
                files=p(scanned, "files"),
            )
        parts.append(
            '<div class="empty">'
            f'<span class="empty-tag">{t("empty.no_ci")}</span>'
            f"<h3>{t('empty.title')}</h3>"
            f"<p>{message}</p>"
            f"<p>{t('empty.body')}</p>"
            "<pre><code>python3 site/build.py --results results</code></pre>"
            "</div>"
        )
        parts.append(run_callout(None))
        if ignored or invalid:
            notes = []
            if ignored:
                notes.append(
                    t(
                        "sources.ignored",
                        n=len(ignored),
                        files=p(len(ignored), "files"),
                    )
                )
            if invalid:
                notes.append(
                    t(
                        "sources.unread",
                        n=len(invalid),
                        files=p(len(invalid), "files"),
                    )
                )
            parts.append(f'<p class="details-note">{" ".join(notes)}</p>')
    return section(
        "latest",
        t("section.01.number"),
        t("section.01.title"),
        t("section.01.lede"),
        "".join(parts),
    )


# --------------------------------------------------------------------------
# results section


def legend_rtt() -> str:
    return (
        f'<div class="legend" aria-label="{esc(t("legend.aria"))}">'
        '<span><i class="swatch" style="background:var(--ink-2)"></i>RTT ≈ 0</span>'
        '<span><i class="swatch tint-neutral"></i>RTT 50 ms</span>'
        "</div>"
    )


def render_results_section(groups):
    rtt0_groups = [group for group in groups if group.rec("0") is not None]
    rtt_groups = [
        group
        for group in groups
        if group.rec("0") is not None or group.rec("50") is not None
    ]
    has_rtt50 = any(group.rec("50") is not None for group in rtt_groups)

    if not groups:
        body = (
            '<div class="empty">'
            f'<span class="empty-tag">{t("results.nothing_tag")}</span>'
            f"<h3>{t('results.nothing_title')}</h3>"
            f"<p>{t('results.nothing_body')}</p>"
            "</div>"
        )
        return section(
            "results",
            t("section.02.number"),
            t("section.02.title"),
            t("section.02.lede"),
            body,
        )

    parts = []
    parts.append(f'<p class="sample-line">{t("results.sample")}</p>')
    parts.append(f'<p class="table-hint">{t("results.table_hint")}</p>')

    if rtt0_groups:
        parts.append(
            chart_block(
                "chart-throughput",
                t("chart.throughput.title"),
                t("chart.throughput.sub"),
                throughput_chart(rtt0_groups),
                t("chart.throughput.foot"),
                table_throughput(rtt0_groups),
            )
        )
    else:
        parts.append(
            f'<p class="missing-note">{t("results.missing_rtt0")}</p>'
        )

    if has_rtt50:
        parts.append(
            chart_block(
                "chart-rtt50",
                t("chart.rtt.title"),
                t("chart.rtt.sub"),
                rtt_chart(rtt_groups),
                t("chart.rtt.foot"),
                table_rtt(rtt_groups),
                legend=legend_rtt(),
                extra_class="chart-block-rtt",
            )
        )
    else:
        parts.append(
            f'<p class="missing-note">{t("results.missing_rtt50")}</p>'
        )

    if rtt0_groups:
        ttfb_values = []
        conn_values = []
        for group in rtt0_groups:
            record = group.rec("0")
            for field in ("median_ms", "p95_ms"):
                value = metric_value(record, "ttfb", field)
                if value is not None:
                    ttfb_values.append(value)
            value = metric_value(record, "conns", "conns_per_sec")
            if value is not None:
                conn_values.append(value)

        if ttfb_values:
            parts.append(
                chart_block(
                    "chart-ttfb",
                    t("chart.ttfb.title"),
                    t("chart.ttfb.sub"),
                    ttfb_chart(rtt0_groups),
                    t("chart.ttfb.foot"),
                    table_ttfb(rtt0_groups),
                    extra_class="chart-ttfb",
                )
            )
        else:
            parts.append(
                f'<p class="missing-note">{t("results.missing_ttfb")}</p>'
            )

        if conn_values:
            parts.append(
                chart_block(
                    "chart-conns",
                    t("chart.conns.title"),
                    t("chart.conns.sub"),
                    conns_chart(rtt0_groups),
                    t("chart.conns.foot"),
                    table_conns(rtt0_groups),
                )
            )
        else:
            parts.append(
                f'<p class="missing-note">{t("results.missing_conns")}</p>'
            )

    return section(
        "results",
        t("section.02.number"),
        t("section.02.title"),
        t("section.02.lede"),
        "".join(parts),
    )



# --------------------------------------------------------------------------
# cost and evidence section


def cost_cpu_table(group: Group) -> str:
    rows = []
    for record in sorted(
        group.records.values(),
        key=lambda item: (item.rtt is None, item.rtt if item.rtt is not None else 0, item.name),
    ):
        for key in PHASES:
            sys = phase_sys(record, key)
            cpu = sys.get("cpu") if isinstance(sys.get("cpu"), dict) else {}
            if not cpu:
                continue
            phase = metric(record, key)
            runs = phase_runs(record, key)
            busy = cpu.get("busy_ms") if is_num(cpu.get("busy_ms")) else None
            user = cpu.get("user_ms") if is_num(cpu.get("user_ms")) else None
            system = cpu.get("system_ms") if is_num(cpu.get("system_ms")) else None
            softirq = cpu.get("softirq_ms") if is_num(cpu.get("softirq_ms")) else None
            irq = cpu.get("irq_ms") if is_num(cpu.get("irq_ms")) else None
            nproc = phase_nproc(record, cpu)
            wall = phase_wall_seconds(record, key)
            cores = None
            percent = None
            if busy is not None and wall is not None and wall > 0:
                cores = busy / 1000.0 / wall
                if nproc:
                    percent = busy / (wall * 1000.0 * nproc) * 100.0
            phase_meta = rtt_text(record.rtt)
            if nproc is not None:
                phase_meta += f" · {fmt_int(nproc)} vCPU"
            phase_html = (
                f'<span class="cost-phase">{esc(phase_label(key))}</span>'
                f'<span class="cost-phase-meta">{esc(phase_meta)}</span>'
            )
            rows.append(
                "<tr>"
                f'<th scope="row">{phase_html}</th>'
                f'<td class="num">{fmt_int(runs)}</td>'
                f'<td class="num">{fmt_num(busy, 1)}</td>'
                f'<td class="num">{fmt_num(wall, 3)}</td>'
                f'<td class="num">{fmt_num(cores, 2)}</td>'
                f'<td class="num">{fmt_num(percent, 1)}</td>'
                f'<td class="num">{fmt_num(user, 1)}</td>'
                f'<td class="num">{fmt_num(system, 1)}</td>'
                f'<td class="num">{fmt_num(softirq, 1)}</td>'
                f'<td class="num">{fmt_num(irq, 1)}</td>'
                "</tr>"
            )
    if not rows:
        return f'<p class="cost-note">{t("cost.cpu.none")}</p>'
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("cost.cpu.aria"))}">'
        '<table class="t-cost">'
        f"<caption>{t('cost.cpu.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("cost.col.phase")}</th>'
        f'<th scope="col">{t("cost.col.runs")}</th>'
        f'<th scope="col">{t("cost.col.busy")}</th>'
        f'<th scope="col">{t("cost.col.wall")}</th>'
        f'<th scope="col">{t("cost.col.cores")}</th>'
        f'<th scope="col">{t("cost.col.cpu_pct")}</th>'
        f'<th scope="col">{t("cost.col.user")}</th>'
        f'<th scope="col">{t("cost.col.system")}</th>'
        f'<th scope="col">{t("cost.col.softirq")}</th>'
        f'<th scope="col">{t("cost.col.irq")}</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def cost_evidence_table(group: Group) -> str:
    rows = []
    for record in sorted(
        group.records.values(),
        key=lambda item: (item.rtt is None, item.rtt if item.rtt is not None else 0, item.name),
    ):
        for key in PHASES:
            evidence = phase_evidence(record, key)
            if not evidence:
                continue
            assessment = evidence_assessment(record, key)
            numbers = assessment
            bottleneck = assessment["bottleneck"]
            phase_html = (
                f'<span class="cost-phase">{esc(phase_label(key))}</span>'
                f'<span class="cost-phase-meta">{esc(rtt_text(record.rtt))}</span>'
            )
            row_class = ' class="is-bottleneck"' if bottleneck else ""
            rows.append(
                f"<tr{row_class}>"
                f'<th scope="row">{phase_html}</th>'
                f'<td class="num">{fmt_int(numbers["dropped"])}</td>'
                f'<td class="num">{fmt_num(numbers["drop_rate_pct"], 3)}%</td>'
                f'<td class="num">{fmt_int(numbers["overlimits"])}</td>'
                f'<td class="num">{fmt_int(numbers["requeues"])}</td>'
                f'<td class="num">{fmt_int(numbers["backlog_packets"] if numbers["backlog_packets"] is not None else numbers["backlog"])}</td>'
                f'<td class="num">{fmt_int(numbers["retrans"])}</td>'
                f'<td class="num">{fmt_int(numbers["sockets"])}</td>'
                "</tr>"
            )
    if not rows:
        return f'<p class="cost-note">{t("cost.evidence.none")}</p>'
    return (
        '<div class="table-wrap" role="region" tabindex="0" '
        f'aria-label="{esc(t("cost.evidence.aria"))}">'
        '<table class="t-evidence">'
        f"<caption>{t('cost.evidence.caption')}</caption>"
        "<thead><tr>"
        f'<th scope="col">{t("cost.col.phase")}</th>'
        f'<th scope="col">{t("cost.col.qdisc_drops")}</th>'
        f'<th scope="col">{t("cost.col.drop_rate")}</th>'
        f'<th scope="col">{t("cost.col.overlimits")}</th>'
        f'<th scope="col">{t("cost.col.requeues")}</th>'
        f'<th scope="col">{t("cost.col.backlog")}</th>'
        f'<th scope="col">{t("cost.col.retrans")}</th>'
        f'<th scope="col">{t("cost.col.sockets")}</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def raw_counters_details(group: Group) -> str:
    items = []
    for record in sorted(
        group.records.values(),
        key=lambda item: (item.rtt is None, item.rtt if item.rtt is not None else 0, item.name),
    ):
        for key in PHASES:
            evidence = phase_evidence(record, key)
            if not evidence:
                continue
            qdisc = evidence.get("qdisc") if isinstance(evidence.get("qdisc"), dict) else {}
            sock = evidence.get("sock") if isinstance(evidence.get("sock"), dict) else {}
            for kind, raw in (("qdisc", qdisc.get("qdisc_raw")), ("socket", sock.get("sock_raw"))):
                if not isinstance(raw, str) or not raw.strip():
                    continue
                items.append(
                    '<div class="raw-item">'
                    f'<span class="raw-label">{esc(kind)} · {esc(phase_label(key))} · '
                    f"{esc(rtt_text(record.rtt))}</span>"
                    f"<pre class=\"raw-value\"><code>{esc(truncate_text(raw, 360))}</code></pre>"
                    "</div>"
                )
    if not items:
        return ""
    return (
        '<details class="raw-details">'
        f"<summary>{t('cost.raw.summary')}</summary>"
        + "".join(items)
        + "</details>"
    )


def cost_card(group: Group) -> str:
    records = list(group.records.values())
    ram = ram_summary(records)
    rss = rss_summary(records)
    if is_num(ram["total"]) or is_num(ram["available"]):
        source = t(
            "cost.ram.machine" if ram["source"] == "machine" else "cost.ram.free"
        )
        ram_chip = (
            f'<span class="cost-chip">'
            f"{t('cost.ram.chip', free=esc(fmt_kb(ram['available'])), source=esc(source), total=esc(fmt_kb(ram['total'])))}"
            "</span>"
        )
    else:
        ram_chip = f'<span class="cost-chip">{t("cost.ram.empty")}</span>'
    if rss["server"] is not None or rss["client"] is not None:
        rss_chip = (
            f'<span class="cost-chip">'
            f"{t('cost.rss.chip', server=esc(fmt_kb(rss['server'])), client=esc(fmt_kb(rss['client'])))}"
            "</span>"
        )
    else:
        rss_chip = f'<span class="cost-chip">{t("cost.rss.empty")}</span>'

    tag = (
        f'<span class="mini-tag">{t("run.mixed_pair")}</span>' if group.mixed else ""
    )
    extra = " is-invalid" if group_is_invalid(group) else ""
    info = validity_info(records)
    evidence_summary = group_evidence(group)
    unknown_note = ""
    if info["status"] == "unknown":
        unknown_note = (
            '<p class="cost-note">'
            f"{esc(validity_unknown_text(info))}</p>"
        )
    if evidence_summary["has"] and evidence_summary["loss_unknown"]:
        unknown_note += f'<p class="cost-note">{t("cost.loss_note")}</p>'
    return (
        f'<article class="cost-card{extra}">'
        '<div class="cost-head">'
        f"{group_swatch(group)}"
        f'<h3 class="cost-name">{esc(group.display)}</h3>'
        f"{tag}"
        f"{validity_badge(records)}"
        f'<span class="group-meta">{esc(group.meta)}</span>'
        f"{evidence_line(group, compact=True)}"
        "</div>"
        f"{validity_notice(records)}{unknown_note}"
        f'<div class="cost-summary">{ram_chip}{rss_chip}</div>'
        f"<h4>{t('cost.h4.cpu')}</h4>"
        f"{cost_cpu_table(group)}"
        f"<h4>{t('cost.h4.evidence')}</h4>"
        f"{cost_evidence_table(group)}"
        f"{raw_counters_details(group)}"
        "</article>"
    )


def render_cost_section(groups):
    if not groups:
        return section(
            "cost",
            t("section.03.number"),
            t("section.03.title"),
            t("section.03.lede"),
            f'<p class="missing-note">{t("cost.missing")}</p>',
        )

    bottleneck = [group for group in groups if group_evidence(group)["bottleneck"]]
    has_panels = any(
        any_cpu_rows(group) or any_evidence_rows(group) for group in groups
    )
    parts = (
        [f'<p class="table-hint">{t("cost.table_hint")}</p>']
        if has_panels
        else []
    )
    if bottleneck:
        items = []
        for group in bottleneck:
            summary = group_evidence(group)
            reasons = summary["reasons"]
            reason_text = (
                "; ".join(reasons[:3]) if reasons else t("cost.callout.fallback")
            )
            if len(reasons) > 3:
                reason_text += "; " + t("cost.callout.more", n=len(reasons) - 3)
            rate_text = (
                f"{fmt_num(summary['drop_rate_pct'], 3)}%"
                if summary["drop_rate_pct"] is not None
                else "—%"
            )
            items.append(
                f"<li><strong>{esc(group.display)}</strong> — "
                f"{esc(t('cost.callout.item', drops=fmt_int(summary['dropped']), rate=rate_text, reasons=reason_text))}"
                "</li>"
            )
        parts.append(
            f'<aside class="callout evidence-callout" aria-label="{esc(t("cost.callout.aria"))}">'
            '<span class="callout-mark" aria-hidden="true">!</span>'
            "<div>"
            f"<strong>{t('cost.callout.title')}</strong>"
            f"<p>{t('cost.callout.intro')}</p>"
            f'<ul class="evidence-list">{"".join(items)}</ul>'
            f"<p>{t('cost.callout.body')}</p></div></aside>"
        )
    if not any(any_cpu_rows(group) for group in groups) and not any(
        any_evidence_rows(group) for group in groups
    ):
        parts.append(
            f'<p class="missing-note">{t("cost.predate")}</p>'
        )
    parts.extend(cost_card(group) for group in groups)
    return section(
        "cost",
        t("section.03.number"),
        t("section.03.title"),
        t("section.03.lede"),
        "".join(parts),
    )


# --------------------------------------------------------------------------
# reference section


def ref_get(data: dict, section_name: str, engine: str, key: str):
    section_data = data.get(section_name)
    if not isinstance(section_data, dict):
        return None
    engine_data = section_data.get(engine)
    if not isinstance(engine_data, dict):
        return None
    value = engine_data.get(key)
    return float(value) if is_num(value) else None


def engine_label(data: dict, engine: str, fallback: str) -> str:
    engines = data.get("engines")
    if isinstance(engines, list):
        for entry in engines:
            if not isinstance(entry, dict) or entry.get("id") != engine:
                continue
            label = entry.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
    return fallback


def tight_units(text: str) -> str:
    """Keep a value and its unit on the same line."""
    for unit in (" MB/s", " ms", " conn/s", " vCPU"):
        text = text.replace(unit, "\u00a0" + unit[1:])
    return text


def reference_story(data: dict) -> str:
    label = engine_label(data, "unbihex_stock", "Unbihex")
    base = ref_get(data, "rtt0", "unbihex_stock", "down1")
    shaped = ref_get(data, "rtt50", "unbihex_stock", "down1")
    xray50 = ref_get(data, "rtt50", "xray", "down1")
    singbox50 = ref_get(data, "rtt50", "singbox", "down1")
    conns = ref_get(data, "rtt0", "unbihex_stock", "conns_per_sec")
    ttfb_p95 = ref_get(data, "rtt0", "unbihex_stock", "ttfb_p95")
    xray_conns = ref_get(data, "rtt0", "xray", "conns_per_sec")
    singbox_conns = ref_get(data, "rtt0", "singbox", "conns_per_sec")
    xray_p95 = ref_get(data, "rtt0", "xray", "ttfb_p95")
    singbox_p95 = ref_get(data, "rtt0", "singbox", "ttfb_p95")

    sentences = []
    if base is not None and shaped is not None:
        drop = ""
        if shaped > 0 and base / shaped >= 10:
            drop = t("ref.story.drop", ratio=fmt_num(base / shaped, 0))
        compare = ""
        if xray50 is not None and singbox50 is not None:
            compare = t(
                "ref.story.holds_both",
                xray=fmt_num(xray50, 1),
                singbox=fmt_num(singbox50, 1),
            )
        elif xray50 is not None:
            compare = t("ref.story.holds_xray", xray=fmt_num(xray50, 1))
        elif singbox50 is not None:
            compare = t("ref.story.holds_singbox", singbox=fmt_num(singbox50, 1))
        sentences.append(
            t(
                "ref.story.falls",
                label=label,
                base=fmt_num(base, 1),
                shaped=fmt_num(shaped, 1),
            )
            + drop
            + compare
            + "."
        )

    if conns is not None and ttfb_p95 is not None:
        rivals_conns = [
            value for value in (xray_conns, singbox_conns) if value is not None
        ]
        rivals_p95 = [value for value in (xray_p95, singbox_p95) if value is not None]
        if (
            rivals_conns
            and rivals_p95
            and conns < min(rivals_conns)
            and ttfb_p95 > max(rivals_p95)
        ):
            sentences.append(
                t(
                    "ref.story.rank",
                    label=label,
                    conns=fmt_num(conns, 1),
                    ttfb=fmt_num(ttfb_p95, 2),
                )
            )
        else:
            sentences.append(
                t(
                    "ref.story.neutral",
                    label=label,
                    conns=fmt_num(conns, 1),
                    ttfb=fmt_num(ttfb_p95, 2),
                )
            )

    return tight_units(" ".join(sentences))


def meta_value(value: str) -> str:
    """Translate the two descriptive metadata values committed in data.json."""
    if LANG != "ru":
        return value
    match = re.fullmatch(r"(\d+) cores", value.strip())
    if match:
        return t("ref.value.cores", n=match.group(1))
    if value.strip() == "netem on the tunnel port only":
        return t("ref.value.rtt_method")
    return value


def reference_meta(data: dict) -> str:
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    chips = []
    release = source.get("release")
    if isinstance(release, str) and release:
        chips.append((t("ref.meta.release"), release))
    commit = source.get("provenance_commit")
    if isinstance(commit, str) and commit:
        chips.append((t("ref.meta.base"), commit))
    date = source.get("date")
    if isinstance(date, str) and date:
        chips.append((t("ref.meta.date"), date))
    machine = source.get("machine")
    if isinstance(machine, str) and machine:
        chips.append((t("ref.meta.machine"), meta_value(machine)))
    path = source.get("path")
    if isinstance(path, str) and path:
        chips.append((t("ref.meta.path"), path))
    rtt_method = source.get("rtt_method")
    if isinstance(rtt_method, str) and rtt_method:
        chips.append((t("ref.meta.rtt"), meta_value(rtt_method)))
    chips.append((t("ref.meta.profile"), t("ref.meta.profile_value")))
    if not chips:
        return ""
    return (
        '<div class="ref-meta">'
        + "".join(
            f'<span><b>{esc(label)}</b> {esc(value)}</span>' for label, value in chips
        )
        + "</div>"
    )


REFERENCE_FIGURES = (
    ("throughput-rtt0.svg", "ref.fig.rtt0", 440),
    ("throughput-rtt50.svg", "ref.fig.rtt50", 456),
    ("connections.svg", "ref.fig.connections", 428),
    ("topology.svg", "ref.fig.topology", 252),
)


def reference_source_files(data: dict) -> list:
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    listed = source.get("files") if isinstance(source.get("files"), list) else []
    engines = data.get("engines") if isinstance(data.get("engines"), list) else []
    referenced = {
        entry.get(key)
        for entry in engines
        if isinstance(entry, dict)
        for key in ("source_rtt0", "source_rtt50")
        if isinstance(entry.get(key), str) and entry.get(key)
    }
    if not referenced:
        return listed
    return [path for path in listed if isinstance(path, str) and path in referenced]


def render_reference_section(data, copied_charts):
    source_files = reference_source_files(data)
    english_charts = copied_charts.get("en", [])
    russian_charts = copied_charts.get("ru", [])

    parts = []
    parts.append(
        f'<aside class="callout callout-quiet" aria-label="{esc(t("ref.callout.aria"))}">'
        '<span class="callout-mark" aria-hidden="true">16</span>'
        "<div>"
        f"<strong>{t('ref.callout.title')}</strong>"
        f"<p>{t('ref.callout.body')}</p>"
        "</div></aside>"
    )
    parts.append(reference_meta(data))

    if data:
        story = reference_story(data)
        if story:
            parts.append(f'<p class="ref-story">{story}</p>')
    else:
        parts.append(f'<p class="missing-note">{t("ref.meta_missing")}</p>')

    figures = []
    fell_back = False
    base = "" if LANG == "en" else "../"
    for filename, key, height in REFERENCE_FIGURES:
        if LANG == "ru" and filename in russian_charts:
            source = f"{base}assets/charts/ru/{filename}"
        elif filename in english_charts:
            source = f"{base}assets/charts/{filename}"
            fell_back = LANG == "ru"
        else:
            continue
        figures.append(
            '<figure class="ref-figure">'
            f'<img src="{source}" alt="{esc(t(key + ".alt"))}" '
            f'width="880" height="{height}" loading="lazy" decoding="async">'
            f"<figcaption>{esc(t(key + '.caption'))}</figcaption>"
            "</figure>"
        )
    if LANG == "ru" and fell_back:
        parts.append(
            f'<p class="missing-note">{t("ref.charts_fallback")}</p>'
        )
    if figures:
        parts.append("".join(figures))
    else:
        parts.append(f'<p class="missing-note">{t("ref.no_svg")}</p>')

    if source_files:
        items = "".join(
            f"<li>{esc(path)}</li>"
            for path in source_files
            if isinstance(path, str)
        )
        parts.append(
            '<details class="sources">'
            f"<summary>{t('ref.sources.summary')}</summary>"
            f'<ul class="source-list">{items}</ul>'
            f'<p class="details-note">{t("ref.sources.note")}</p>'
            "</details>"
        )

    return section(
        "reference",
        t("section.04.number"),
        t("section.04.title"),
        t("section.04.lede"),
        "".join(parts),
    )


# --------------------------------------------------------------------------
# caveats + footer


CAVEATS = tuple(f"caveat.{index}" for index in range(1, 10))


def render_caveats_section():
    items = "".join(
        f"<li><strong>{t(key + '.title')}</strong>{t(key + '.body')}</li>"
        for key in CAVEATS
    )
    return section(
        "caveats",
        t("section.05.number"),
        t("section.05.title"),
        t("section.05.lede"),
        f'<ul class="caveats">{items}</ul>',
    )


def render_footer():
    return (
        '<footer class="footer">'
        f"<p>{t('footer.line1')}</p>"
        "<p>"
        f"{t('footer.built')} "
        f'<a href="{REPO_URL}">{t("link.repo")}</a> · '
        f'<a href="{README_EN_URL}">{t("link.readme_en")}</a> · '
        f'<a href="{README_RU_URL}">{t("link.readme_ru")}</a> · '
        f'<a href="{WORKFLOW_URL}">{t("link.workflow")}</a> · '
        f'<a href="{CI_RESULTS_URL}">{t("link.raw")}</a>.</p>'
        f'<p class="footer-note">{t("footer.note")}</p>'
        "</footer>"
    )


# --------------------------------------------------------------------------
# page assembly


ROUTING_SCRIPT = """(function(){try{
var K="xb-lang",F="xb-lang-routed",page="__PAGE__",other="__OTHER__";
try{var c=localStorage.getItem(K);if(c)return}catch(e){}
try{if(sessionStorage.getItem(F))return}catch(e){}
var l=(navigator.languages&&navigator.languages[0])||navigator.language||"";
var want=l.toLowerCase().indexOf("ru")===0?"ru":"en";
if(want!==page){try{sessionStorage.setItem(F,"1")}catch(e){}location.replace(other)}
}catch(e){}})();"""


def routing_script(page: str, other: str) -> str:
    script = ROUTING_SCRIPT.replace("__PAGE__", page).replace("__OTHER__", other)
    return f"<script>{script}</script>"


def lang_switch(page: str) -> str:
    en_href = "./" if page == "en" else "../"
    ru_href = "ru/" if page == "en" else "./"
    links = []
    for code, href in (("en", en_href), ("ru", ru_href)):
        current = ' aria-current="true"' if code == page else ""
        click = (
            "try{localStorage.setItem('xb-lang','" + code + "')}catch(e){}"
        )
        links.append(
            f'<a href="{href}" hreflang="{code}" lang="{code}" '
            f'title="{esc(t("lang." + code + "_title"))}"'
            f'{current} onclick="{esc(click)}">{t("lang." + code)}</a>'
        )
    return (
        f'<nav class="lang-switch" aria-label="{esc(t("lang.label"))}">'
        + '<span aria-hidden="true">/</span>'.join(links)
        + "</nav>"
    )


def section(section_id: str, number: str, title: str, lede: str, body: str) -> str:
    return (
        f'<section id="{section_id}" aria-labelledby="{section_id}-title">'
        '<div class="section-head">'
        f'<span class="sec-no" aria-hidden="true">{number}</span>'
        "<div>"
        f'<h2 id="{section_id}-title">{title}</h2>'
        f'<p class="section-lede">{lede}</p>'
        "</div></div>"
        f"{body}</section>"
    )


def render_hero(page: str) -> str:
    return f"""
<header class="hero">
  <div class="hero-bar">
    <p class="kicker"><span class="dot" aria-hidden="true"></span>xray-bench
      <span class="sep" aria-hidden="true">/</span>{t("hero.kicker")}
      <span class="kicker-extra"><span class="sep" aria-hidden="true">/</span>
        REALITY · XHTTP</span></p>
    {lang_switch(page)}
  </div>
  <div class="hero-grid">
    <div class="hero-copy">
      <h1>{t("hero.title")}</h1>
      <p class="lede">{t("hero.lede")}</p>
      <p class="hero-note">{t("hero.note")}</p>
      <nav class="hero-links" aria-label="{esc(t("hero.links_aria"))}">
        <a href="{REPO_URL}">{t("link.repo")}</a>
        <span class="sep" aria-hidden="true">·</span>
        <a href="{README_EN_URL}">{t("link.readme_en")}</a>
        <span class="sep" aria-hidden="true">·</span>
        <a href="{README_RU_URL}">{t("link.readme_ru")}</a>
        <span class="sep" aria-hidden="true">·</span>
        <a href="{WORKFLOW_URL}">{t("link.workflow")}</a>
        <span class="sep" aria-hidden="true">·</span>
        <a href="{CI_RESULTS_URL}">{t("link.raw")}</a>
      </nav>
    </div>
    <aside class="spec" aria-label="{esc(t("hero.spec_aria"))}">
      <dl>
        <div><dt>{t("spec.path.label")}</dt><dd>{t("spec.path.value")}</dd></div>
        <div><dt>{t("spec.link.label")}</dt><dd>{t("spec.link.value")}</dd></div>
        <div><dt>{t("spec.measures.label")}</dt><dd>{t("spec.measures.value")}</dd></div>
        <div><dt>{t("spec.rule.label")}</dt><dd>{t("spec.rule.value")}</dd></div>
      </dl>
    </aside>
  </div>
</header>
"""


def build_page(groups, records, selected, run, ignored, invalid, scanned, state, data, copied_charts, lang):
    set_lang(lang)
    page = LANG
    base = "" if page == "en" else "../"
    other = "ru/" if page == "en" else "../"
    canonical = SITE_URL if page == "en" else SITE_URL + "ru/"
    return f"""<!doctype html>
<html lang="{page}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f2eee5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0f13" media="(prefers-color-scheme: dark)">
<meta name="description" content="{esc(t("meta.description"))}">
<title>{esc(t("meta.title"))}</title>
{routing_script(page, other)}
<link rel="canonical" href="{canonical}">
<link rel="alternate" hreflang="en" href="{SITE_URL}">
<link rel="alternate" hreflang="ru" href="{SITE_URL}ru/">
<link rel="alternate" hreflang="x-default" href="{SITE_URL}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230e7490'/%3E%3Crect x='7' y='17' width='5' height='9' fill='%23fff'/%3E%3Crect x='14' y='11' width='5' height='15' fill='%23fff'/%3E%3Crect x='21' y='7' width='5' height='19' fill='%23d97706'/%3E%3C/svg%3E">
<link rel="stylesheet" href="{base}assets/style.css">
</head>
<body>
<a class="skip" href="#main">{t("page.skip")}</a>
<div class="page">
{render_hero(page)}
<main id="main">
{render_run_section(records, selected, groups, run, ignored, invalid, scanned, state)}
{render_results_section(groups)}
{render_cost_section(groups)}
{render_reference_section(data, copied_charts)}
{render_caveats_section()}
</main>
{render_footer()}
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# stylesheet


STYLESHEET = r"""/* xray-bench dashboard — generated by site/build.py */
*,*::before,*::after{box-sizing:border-box}
:root{
  --max:1160px;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
  --sans:"Segoe UI","Helvetica Neue",system-ui,-apple-system,Roboto,Arial,sans-serif;
  --mono:"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --bg:#f2eee5;
  --bg-2:#e9e2d5;
  --surface:#fffdf8;
  --surface-2:#f7f2e7;
  --ink:#1c1a16;
  --ink-2:#4e473d;
  --muted:#72695c;
  --line:#d8d0c1;
  --line-2:#b8ad9a;
  --grid:rgba(28,26,22,.10);
  --accent:#0e7490;
  --accent-2:#b45309;
  --warn-bg:#fff3d8;
  --warn-line:#d9a53f;
  --warn-ink:#5c4304;
  --ok:#166534;
  --ok-bg:#eaf5ee;
  --ok-line:#a7d3b5;
  --code-bg:#eee7d9;
  --ref-bg:#ffffff;
  --focus:#0e7490;
  --shadow:0 1px 0 rgba(28,26,22,.03);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0b0f13;
    --bg-2:#0e141a;
    --surface:#12181e;
    --surface-2:#161e26;
    --ink:#ece7dd;
    --ink-2:#c9c1b4;
    --muted:#98a1aa;
    --line:#26313b;
    --line-2:#3d4b57;
    --grid:rgba(236,231,221,.10);
    --accent:#31b7d6;
    --accent-2:#f0a02a;
    --warn-bg:#291f10;
    --warn-line:#8a641f;
    --warn-ink:#f2d69e;
    --ok:#86efac;
    --ok-bg:#0f2419;
    --ok-line:#2f6b45;
    --code-bg:#1a222b;
    --ref-bg:#0d1117;
    --focus:#6ad4ef;
    --shadow:none;
  }
}
html{scroll-behavior:smooth}
body{
  margin:0;
  background:
    radial-gradient(900px 520px at 88% -8%, rgba(14,116,144,.12), transparent 62%),
    radial-gradient(760px 480px at -5% 4%, rgba(180,83,9,.09), transparent 60%),
    var(--bg);
  color:var(--ink);
  font-family:var(--sans);
  font-size:16px;
  line-height:1.6;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
@media (prefers-color-scheme: dark){
  body{
    background:
      radial-gradient(900px 520px at 88% -8%, rgba(49,183,214,.10), transparent 62%),
      radial-gradient(760px 480px at -5% 4%, rgba(240,160,42,.07), transparent 60%),
      var(--bg);
  }
}
a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:.18em}
a:hover{color:var(--accent-2)}
:focus-visible{outline:2px solid var(--focus);outline-offset:3px;border-radius:2px}
code,pre{font-family:var(--mono);font-size:.82em;background:var(--code-bg);border:1px solid var(--line);border-radius:2px}
code{padding:.08em .32em}
pre{padding:.85rem 1rem;overflow-x:auto;margin:.6rem 0 0}
pre code{border:0;background:none;padding:0;font-size:1em}
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip{position:absolute;left:1rem;top:-4rem;z-index:20;background:var(--surface);color:var(--ink);border:1px solid var(--line-2);padding:.6rem .9rem;font-family:var(--mono);font-size:.8rem}
.skip:focus{top:1rem}
.page{max-width:var(--max);margin:0 auto;padding:clamp(1.2rem,4vw,3.5rem) clamp(1.05rem,4vw,3rem) 3rem}

/* hero ------------------------------------------------------------------ */
.hero{padding:clamp(.8rem,3vw,2rem) 0 clamp(2.2rem,6vw,4rem);border-bottom:1px solid var(--line)}
.hero-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(17rem,.55fr);gap:clamp(2rem,6vw,5rem);align-items:end}
.hero-copy{min-width:0}
.hero-bar{display:flex;justify-content:space-between;align-items:center;gap:1rem .9rem;flex-wrap:wrap;margin:0 0 1.3rem}
.lang-switch{display:flex;align-items:center;gap:.5rem;font-family:var(--mono);font-size:.64rem;letter-spacing:.12em;text-transform:uppercase}
.lang-switch a{padding:.16rem .42rem;border:1px solid var(--line-2);color:var(--muted);text-decoration:none;line-height:1.3}
.lang-switch a:hover{color:var(--ink);border-color:var(--ink-2)}
.lang-switch a[aria-current]{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.kicker{display:flex;flex-wrap:wrap;align-items:center;gap:.55rem;flex:1 1 12rem;min-width:0;margin:0;font-family:var(--mono);font-size:.68rem;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}
.kicker .dot{width:.42rem;height:.42rem;border-radius:50%;background:var(--accent-2);flex:none}
.kicker .sep{color:var(--line-2)}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(2.35rem,4.8vw,3.35rem);line-height:1;letter-spacing:-.03em;margin:1.1rem 0 1.2rem;text-wrap:balance}
h1 .h1-name{white-space:nowrap}
.lede{max-width:40rem;margin:0 0 1.15rem;font-size:clamp(1rem,1.6vw,1.18rem);line-height:1.55;color:var(--ink-2)}
.hero-note{max-width:40rem;margin:0 0 1.5rem;padding-left:.8rem;border-left:2px solid var(--accent-2);font-family:var(--mono);font-size:.7rem;line-height:1.55;color:var(--muted)}
.hero-links{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem .7rem;font-size:.84rem;line-height:1.5}
.hero-links a{text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--accent) 35%,transparent);padding-bottom:.05rem}
.hero-links a:hover{border-bottom-color:var(--accent-2)}
.hero-links .sep{color:var(--line-2);font-family:var(--mono)}
.spec{border:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow);padding:1.15rem 1.2rem;min-width:0}
.spec dl{display:grid;gap:.9rem;margin:0}
.spec dl div{display:grid;gap:.12rem;min-width:0}
.spec dt{font-family:var(--mono);font-size:.6rem;letter-spacing:.13em;text-transform:uppercase;color:var(--muted)}
.spec dd{margin:0;font-family:var(--mono);font-size:.76rem;line-height:1.45;color:var(--ink);overflow-wrap:anywhere}

/* page load motion ------------------------------------------------------ */
.hero .kicker,.hero h1,.hero .lede,.hero .hero-note,.hero .hero-links,.hero .spec{animation:rise .72s cubic-bezier(.2,.7,.2,1) both}
.hero .kicker{animation-delay:.02s}
.hero h1{animation-delay:.08s}
.hero .lede{animation-delay:.16s}
.hero .hero-note{animation-delay:.22s}
.hero .hero-links{animation-delay:.28s}
.hero .spec{animation-delay:.2s}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

/* sections -------------------------------------------------------------- */
main section{padding:clamp(2.2rem,6vw,4rem) 0;border-bottom:1px solid var(--line)}
.section-head{display:grid;grid-template-columns:auto minmax(0,1fr);gap:1rem 1.1rem;align-items:start;margin-bottom:1.35rem}
.sec-no{padding-top:.32rem;font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;color:var(--accent)}
.section-head h2{margin:0;font-family:var(--serif);font-weight:600;font-size:clamp(1.8rem,3.6vw,2.6rem);line-height:1.05;letter-spacing:-.02em;text-wrap:balance}
.section-lede{max-width:66ch;margin:.5rem 0 0;color:var(--ink-2);font-size:.94rem;line-height:1.55}

/* callouts -------------------------------------------------------------- */
.callout{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.9rem;margin:1.2rem 0 1.5rem;padding:1rem 1.1rem;border:1px solid var(--warn-line);background:var(--warn-bg);color:var(--warn-ink)}
.callout-mark{display:grid;place-items:center;width:1.55rem;height:1.55rem;border:1px solid currentColor;border-radius:50%;font-family:var(--mono);font-size:.72rem;font-weight:700;line-height:1}
.callout strong{display:block;font-size:.9rem;line-height:1.35;letter-spacing:.01em}
.callout p{margin:.22rem 0 0;font-size:.86rem;line-height:1.55;color:inherit}
.callout-quiet{border-color:var(--line-2);background:var(--surface-2);color:var(--ink-2)}
.callout-quiet .callout-mark{border-radius:2px}
.callout-quiet strong{color:var(--ink)}

/* run summary ----------------------------------------------------------- */
.run-strip{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;margin:0 0 1.2rem}
.chip{display:inline-flex;align-items:baseline;gap:.35rem;padding:.28rem .55rem;border:1px solid var(--line);background:var(--surface);font-family:var(--mono);font-size:.65rem;color:var(--muted);letter-spacing:.03em}
.chip strong{font-size:.78rem;color:var(--ink);font-weight:600}
.meta-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem}
.card{min-width:0;padding:1.05rem 1.1rem;border:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow)}
.card-wide{grid-column:1/-1;margin-top:1rem}
.card h3{margin:0 0 .8rem;font-family:var(--mono);font-size:.61rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
.card-empty{margin:0;color:var(--muted);font-size:.84rem}
.kv{display:grid;gap:.6rem;margin:0}
.kv div{display:grid;gap:.1rem;min-width:0}
.kv dt{font-family:var(--mono);font-size:.59rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);overflow-wrap:anywhere}
.kv dd{margin:0;font-size:.85rem;line-height:1.45;overflow-wrap:anywhere}
.kv dd.mono{font-family:var(--mono);font-size:.72rem;line-height:1.5}
.kv code{font-size:.92em}

/* tables ---------------------------------------------------------------- */
.table-wrap{margin-top:1rem;border:1px solid var(--line);background:var(--surface);overflow-x:auto}
.table-plain{border:0;margin-top:0}
table{width:100%;border-collapse:collapse;font-size:.84rem}
caption{text-align:left;padding:.85rem 1rem .7rem;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:.63rem;line-height:1.5;letter-spacing:.055em;text-transform:uppercase;color:var(--muted)}
th,td{padding:.68rem .85rem;border-bottom:1px solid var(--line);text-align:right;vertical-align:top;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{background:var(--surface-2);font-family:var(--mono);font-size:.6rem;letter-spacing:.075em;text-transform:uppercase;color:var(--muted);font-weight:600}
tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}
tbody tr:hover{background:var(--surface-2)}
.num{font-family:var(--mono);font-size:.78rem;font-variant-numeric:tabular-nums}
.range{display:block;margin-top:.08rem;font-size:.62rem;color:var(--muted)}
.row-name{display:block;font-weight:600;line-height:1.35;white-space:normal}
.row-meta{display:block;margin-top:.12rem;font-family:var(--mono);font-size:.6rem;color:var(--muted);line-height:1.4;white-space:normal}
.t-throughput{min-width:580px}
.t-rtt{min-width:520px}
.t-ttfb{min-width:560px}
.t-conns{min-width:560px}
.t-binaries{min-width:520px}
.t-binaries th,.t-binaries td{text-align:left;white-space:normal}
.t-binaries .bin-version{font-family:var(--mono);font-size:.7rem;line-height:1.5;overflow-wrap:anywhere}
.t-binaries .bin-sha{font-family:var(--mono);font-size:.7rem;white-space:nowrap}

/* charts ---------------------------------------------------------------- */
.chart-block{margin:0 0 2.5rem}
.chart-block:last-child{margin-bottom:0}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;margin-bottom:.85rem}
.chart-head h3{margin:0;font-family:var(--serif);font-weight:600;font-size:clamp(1.28rem,2.2vw,1.62rem);line-height:1.15;letter-spacing:-.01em}
.chart-head p{margin:.2rem 0 0;max-width:62ch;color:var(--muted);font-size:.82rem;line-height:1.5}
.legend{display:flex;flex-wrap:wrap;gap:.9rem;padding-top:.35rem;font-family:var(--mono);font-size:.63rem;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:.38rem}
.swatch{display:inline-block;flex:none;width:.72rem;height:.72rem;border-radius:2px}
.swatch.tint-neutral{background:transparent;box-shadow:inset 0 0 0 1.5px var(--ink-2)}
.chart{--labelw:3.25rem;--valuew:5.4rem}
.chart-ttfb .chart{--valuew:6.3rem}
.axis{display:grid;grid-template-columns:var(--labelw) minmax(0,1fr) var(--valuew);gap:.55rem;align-items:end;margin-bottom:.15rem}
.axis-unit{padding-bottom:.05rem;font-family:var(--mono);font-size:.58rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.axis-plot{position:relative;height:1rem}
.axis-plot .tick{position:absolute;bottom:0;transform:translateX(-50%);font-family:var(--mono);font-size:.6rem;font-variant-numeric:tabular-nums;color:var(--muted)}
.axis-plot .tick.first{transform:none}
.axis-plot .tick.last{transform:translateX(-100%)}
.bar-group{padding:.75rem 0 .45rem}
.bar-group + .bar-group{border-top:1px dashed var(--line)}
.group-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.45rem;margin-bottom:.3rem;min-width:0}
.group-title{display:inline-flex;align-items:center;gap:.45rem;min-width:0;max-width:100%}
.group-name{font-size:.9rem;font-weight:650;line-height:1.3}
.group-meta{font-family:var(--mono);font-size:.6rem;color:var(--muted);overflow-wrap:anywhere}
.mini-tag{padding:.08rem .3rem;border:1px solid var(--line-2);font-family:var(--mono);font-size:.54rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
.bar-row{display:grid;grid-template-columns:var(--labelw) minmax(0,1fr) var(--valuew);gap:.55rem;align-items:center;min-height:1.9rem}
.bar-label{font-family:var(--mono);font-size:.66rem;color:var(--ink-2);white-space:nowrap}
.track{position:relative;height:1rem;border-radius:3px;background-color:var(--surface-2);box-shadow:inset 0 0 0 1px var(--grid);background-image:repeating-linear-gradient(to right,var(--grid) 0 1px,transparent 1px calc(100% / var(--div,1)))}
.track-empty{background-image:repeating-linear-gradient(135deg,transparent 0 6px,var(--line-2) 6px 7px);box-shadow:inset 0 0 0 1px var(--line-2)}
.fill{display:block;height:100%;width:var(--v,0%);min-width:2px;border-radius:3px;background:var(--bar);animation:grow .8s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(var(--i,0) * 45ms)}
.fill.is-zero{min-width:0}
.fill.tint{background:var(--bar-tint);box-shadow:inset 0 0 0 1.5px var(--bar)}
.p95{position:absolute;top:-.19rem;bottom:-.19rem;left:var(--p);width:2px;margin-left:-1px;border-radius:1px;background:var(--bar)}
.bar-value{font-family:var(--mono);font-size:.71rem;line-height:1.25;font-variant-numeric:tabular-nums;color:var(--ink);white-space:nowrap;overflow-wrap:anywhere}
.bar-value small{display:block;font-size:.59rem;font-weight:400;color:var(--muted)}
@keyframes grow{from{width:0}to{width:var(--v,0%)}}
.chart-foot{margin:.7rem 0 0;font-size:.72rem;line-height:1.5;color:var(--muted)}
.sample-line{margin:0 0 1.6rem;font-family:var(--mono);font-size:.69rem;line-height:1.6;color:var(--muted)}
.missing-note{margin:0 0 1.8rem;padding:.85rem 1rem;border-left:2px solid var(--line-2);background:var(--surface-2);font-size:.84rem;color:var(--ink-2)}
.table-hint{display:none;margin:-.9rem 0 1.3rem;font-family:var(--mono);font-size:.63rem;color:var(--muted)}

/* empty state ----------------------------------------------------------- */
.empty{padding:clamp(1.3rem,4vw,2.2rem);border:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow)}
.empty-tag{display:inline-block;margin-bottom:.5rem;font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--accent-2)}
.empty h3{margin:0 0 .4rem;font-family:var(--serif);font-weight:600;font-size:clamp(1.4rem,3vw,1.9rem);line-height:1.15}
.empty p{max-width:66ch;margin:.35rem 0;color:var(--ink-2);font-size:.9rem}
.empty pre{max-width:34rem}
.empty pre code{white-space:pre-wrap;overflow-wrap:anywhere}

/* reference ------------------------------------------------------------- */
.ref-meta{display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 1.1rem}
.ref-meta span{padding:.24rem .5rem;border:1px solid var(--line);background:var(--surface);font-family:var(--mono);font-size:.63rem;color:var(--muted)}
.ref-meta b{font-weight:600;color:var(--ink);letter-spacing:.02em}
.ref-story{max-width:76ch;margin:0 0 1.5rem;font-size:.94rem;line-height:1.65;color:var(--ink-2)}
.ref-figure{margin:1.4rem 0 1.8rem;padding:clamp(.55rem,2vw,1.15rem);border:1px solid var(--line);background:var(--ref-bg)}
.ref-figure img{display:block;width:100%;height:auto}
.ref-figure figcaption{margin-top:.7rem;padding-top:.65rem;border-top:1px solid var(--line);font-family:var(--mono);font-size:.64rem;line-height:1.5;letter-spacing:.03em;color:var(--muted)}

/* caveats --------------------------------------------------------------- */
.caveats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;margin:0;padding:0;list-style:none;border:1px solid var(--line);background:var(--line)}
.caveats li{padding:1rem 1.1rem;background:var(--surface);font-size:.84rem;line-height:1.55;color:var(--ink-2)}
.caveats strong{display:block;margin-bottom:.22rem;font-size:.9rem;color:var(--ink)}
.caveats li:last-child:nth-child(odd){grid-column:1/-1}

/* details --------------------------------------------------------------- */
details.sources{margin-top:1rem;padding:.8rem 1rem;border:1px solid var(--line);background:var(--surface)}
details.sources summary{cursor:pointer;font-family:var(--mono);font-size:.64rem;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
details.sources summary:hover{color:var(--ink)}
details.sources[open] summary{margin-bottom:.65rem}
.source-list{display:grid;gap:.22rem;margin:0;padding:0;list-style:none;font-family:var(--mono);font-size:.64rem;line-height:1.5;color:var(--ink-2);overflow-wrap:anywhere}
.source-scenario{color:var(--ink)}
.details-note{margin:.6rem 0 0;font-size:.72rem;line-height:1.55;color:var(--muted)}

/* footer ---------------------------------------------------------------- */
.footer{padding:2rem 0 0;font-size:.78rem;line-height:1.65;color:var(--muted)}
.footer p{margin:.3rem 0}
.footer strong{color:var(--ink)}
.footer-note{font-family:var(--mono);font-size:.66rem}

/* profile, validity, evidence ------------------------------------------- */
.profile-chip{display:inline-flex;align-items:center;padding:.16rem .45rem;border:1px solid var(--line-2);background:var(--surface);color:var(--muted);font-family:var(--mono);font-size:.58rem;letter-spacing:.08em;text-transform:uppercase;line-height:1.3;white-space:nowrap}
.profile-wan{color:var(--accent-2);border-color:var(--accent-2)}
.profile-loopback{color:var(--accent);border-color:var(--accent)}
.profile-none{color:var(--muted);background:var(--surface-2)}
.run-strip .profile-chip{padding:.28rem .55rem;background:var(--surface)}

.badge{display:inline-flex;align-items:center;padding:.12rem .4rem;border:1px solid var(--line-2);background:var(--surface-2);color:var(--muted);font-family:var(--mono);font-size:.54rem;letter-spacing:.07em;text-transform:uppercase;line-height:1.2;white-space:nowrap}
.badge-valid{color:var(--ok);border-color:var(--ok-line);background:var(--ok-bg)}
.badge-invalid{color:var(--warn-ink);border-color:var(--warn-line);background:var(--warn-bg)}
.badge-unknown{color:var(--muted);border-color:var(--line-2);background:var(--surface-2)}
details.validity{display:inline-block;max-width:100%}
details.validity summary{list-style:none;cursor:pointer}
details.validity summary::-webkit-details-marker{display:none}
details.validity summary::marker{content:""}
.validity-pop{margin-top:.4rem;padding:.55rem .65rem;border:1px solid var(--warn-line);background:var(--warn-bg);color:var(--warn-ink);font-size:.7rem;line-height:1.45;max-width:min(30rem,82vw)}
.validity-pop strong{display:block;font-size:.64rem;letter-spacing:.08em;text-transform:uppercase}
.validity-pop ul{margin:.25rem 0 0;padding-left:1rem}
.validity-count{display:block;margin-top:.15rem;font-family:var(--mono);font-size:.58rem;opacity:.9}
.validity-notice{margin:.6rem 0 .8rem;padding:.6rem .7rem;border:1px solid var(--line);border-left:3px solid var(--warn-line);background:var(--surface-2);color:var(--ink-2);font-size:.74rem;line-height:1.5}
.validity-notice > strong{display:block;font-size:.72rem;letter-spacing:.02em;color:var(--warn-ink)}
.validity-notice ul{margin:.25rem 0;padding-left:1.1rem}
.validity-notice p{margin:.2rem 0 0;font-size:.7rem}

.group-evidence{margin-left:.1rem;font-family:var(--mono);font-size:.59rem;line-height:1.45;color:var(--muted);white-space:normal}
.netem-warning{display:inline-block;margin-left:.35rem;padding:.1rem .35rem;border:1px solid var(--warn-line);background:var(--warn-bg);color:var(--warn-ink);font-family:var(--mono);font-size:.54rem;letter-spacing:.04em;text-transform:uppercase;line-height:1.25;white-space:nowrap}
.evidence-note{margin-left:.4rem;font-family:var(--mono);font-size:.56rem;color:var(--muted);white-space:normal}
.evidence-list{margin:.35rem 0 .4rem;padding-left:1.1rem;font-size:.78rem;line-height:1.5}
.evidence-list li{margin:.15rem 0}
.bar-group.is-invalid{border-left:2px solid var(--warn-line);margin-left:-.55rem;padding-left:.55rem}
tbody tr.is-invalid th:first-child{border-left:2px solid var(--warn-line)}
.row-flags{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem;margin-top:.28rem}
.row-flags .group-evidence{font-size:.56rem}
.card-note{margin:.6rem 0 0;font-size:.7rem;line-height:1.5;color:var(--muted)}

/* cost and evidence ------------------------------------------------------ */
.cost-card{margin:0 0 1.2rem;padding:1rem 1.1rem;border:1px solid var(--line);background:var(--surface);box-shadow:var(--shadow)}
.cost-card.is-invalid{border-left:3px solid var(--warn-line)}
.cost-head{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .5rem;margin-bottom:.5rem}
.cost-name{margin:0;font-size:.95rem;font-weight:650;line-height:1.3}
.cost-summary{display:flex;flex-wrap:wrap;gap:.4rem;margin:.55rem 0 .9rem}
.cost-chip{max-width:100%;padding:.22rem .5rem;border:1px solid var(--line);background:var(--surface-2);font-family:var(--mono);font-size:.62rem;line-height:1.4;color:var(--ink-2);overflow-wrap:anywhere}
.cost-chip strong{color:var(--ink);font-weight:600}
.cost-card h4{margin:1rem 0 .5rem;font-family:var(--mono);font-size:.6rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600}
.cost-note{margin:.35rem 0 .7rem;font-size:.74rem;line-height:1.5;color:var(--muted)}
.t-cost{min-width:980px}
.t-evidence{min-width:850px}
.cost-phase{display:block;font-weight:600;color:var(--ink)}
.cost-phase-meta{display:block;margin-top:.1rem;font-family:var(--mono);font-size:.58rem;color:var(--muted);white-space:normal}
.t-cost tr.is-bottleneck th:first-child,.t-evidence tr.is-bottleneck th:first-child{border-left:2px solid var(--warn-line)}
.raw-details{margin-top:.8rem;padding:.7rem .8rem;border:1px solid var(--line);background:var(--surface-2)}
.raw-details summary{cursor:pointer;font-family:var(--mono);font-size:.62rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.raw-details summary:hover{color:var(--ink)}
.raw-details[open] summary{margin-bottom:.55rem}
.raw-item{margin-top:.55rem}
.raw-label{display:block;font-family:var(--mono);font-size:.56rem;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.raw-value{max-height:10rem;margin:.25rem 0 0;padding:.5rem .6rem;border:1px solid var(--line);background:var(--code-bg);font-size:.62rem;line-height:1.45;white-space:pre-wrap;overflow:auto;overflow-wrap:anywhere}

/* responsive ------------------------------------------------------------ */
@media (max-width:900px){
  .hero-grid{grid-template-columns:1fr;align-items:start}
  .spec{max-width:32rem}
  .meta-grid{grid-template-columns:1fr}
  .card-wide{grid-column:auto}
}
@media (max-width:700px){
  .caveats{grid-template-columns:1fr}
  .section-head{gap:.7rem .8rem}
  .section-head h2{font-size:clamp(1.6rem,6vw,2.1rem)}
  .table-hint{display:block}
  .validity-pop{max-width:100%}
  .cost-card{padding:.9rem .85rem}
  .cost-chip{font-size:.58rem}
}
@media (max-width:620px){
  .t-binaries{min-width:0}
  .t-binaries thead{display:none}
  .t-binaries,.t-binaries tbody,.t-binaries tr,.t-binaries th,.t-binaries td{display:block;width:100%}
  .t-binaries tr{padding:.7rem .9rem;border-bottom:1px solid var(--line)}
  .t-binaries tr:last-child{border-bottom:0}
  .t-binaries th,.t-binaries td{padding:.1rem 0;border:0;text-align:left;white-space:normal}
  .t-binaries th{font-family:var(--sans);font-size:.86rem;font-weight:650;color:var(--ink)}
  .t-binaries td.bin-version{font-family:var(--mono);font-size:.68rem;color:var(--ink-2);line-height:1.5}
  .t-binaries td.bin-sha{margin-top:.15rem}
  .t-binaries td.bin-sha::before{content:attr(data-label) " ";font-size:.56rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
}
@media (max-width:520px){
  .page{padding-left:.9rem;padding-right:.9rem}
  .group-evidence{font-size:.55rem}
  .cost-name{font-size:.88rem}
  .cost-card h4{font-size:.56rem}
  .netem-warning{font-size:.5rem}
  .chart{--labelw:2.75rem;--valuew:4.45rem}
  .chart-ttfb .chart{--valuew:5.2rem}
  .bar-label{font-size:.61rem}
  .bar-value{font-size:.65rem}
  .bar-value small{font-size:.55rem}
  .axis-unit{font-size:.53rem;letter-spacing:.02em}
  .axis-plot .tick{font-size:.55rem}
  .group-name{font-size:.84rem}
  .group-meta{font-size:.56rem}
  .chart-head{gap:.4rem}
  .legend{font-size:.58rem}
  .kicker-extra{display:none}
  th,td{padding:.6rem .65rem}
  .callout{grid-template-columns:1fr;gap:.5rem}
  .callout-mark{width:1.35rem;height:1.35rem}
}
@media (max-width:380px){
  .chart{--labelw:2.4rem;--valuew:4.1rem}
  .chart-ttfb .chart{--valuew:4.8rem}
  .bar-row,.axis{gap:.4rem}
  .kicker{font-size:.55rem;letter-spacing:.07em;gap:.4rem}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
  .hero .kicker,.hero h1,.hero .lede,.hero .hero-note,.hero .hero-links,.hero .spec{animation:none;opacity:1;transform:none}
  .fill{animation:none;width:var(--v,0%)}
}
@media print{
  .hero-links,.skip{display:none}
  body{background:#fff;color:#000}
  .ref-figure,.card,.table-wrap,.empty{border-color:#bbb}
  .fill{animation:none;width:var(--v,0%)}
}
"""


# --------------------------------------------------------------------------
# output


def copy_reference_charts(charts_dir: Path, out_dir: Path):
    target = out_dir / "assets" / "charts"
    copied = {"en": [], "ru": []}
    for lang, source_dir in (("en", charts_dir), ("ru", charts_dir / "ru")):
        for filename in CHART_FILES:
            source = source_dir / filename
            if not source.is_file():
                continue
            if lang == "ru":
                (target / "ru").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target / "ru" / filename)
            else:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target / filename)
            copied[lang].append(filename)
    return copied


def load_reference_data(charts_dir: Path) -> dict:
    path = charts_dir / "data.json"
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build(results_dir: Path, charts_dir: Path, out_dir: Path, lang: str = "all"):
    records, scanned, ignored, invalid, state = load_results(results_dir)
    runs = split_runs(records)
    run = runs[-1] if runs else []
    selected = select_latest(run) if run else []
    data = load_reference_data(charts_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"
    if assets.exists():
        if assets.is_dir():
            shutil.rmtree(assets)
        else:
            assets.unlink()
    assets.mkdir(parents=True, exist_ok=True)

    (assets / "style.css").write_text(STYLESHEET, encoding="utf-8", newline="\n")
    copied = copy_reference_charts(charts_dir, out_dir)

    written = []
    languages = ("en", "ru") if lang == "all" else (lang,)
    groups = []
    for code in languages:
        set_lang(code)
        groups = build_groups(selected)
        page = build_page(
            groups,
            records,
            selected,
            run,
            ignored,
            invalid,
            scanned,
            state,
            data,
            copied,
            code,
        )
        target = out_dir / "index.html" if code == "en" else out_dir / "ru" / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8", newline="\n")
        written.append(target)
    return selected, groups, copied, scanned, ignored, invalid, written


def main(argv=None):
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Build the self-contained xray-bench dashboard.",
    )
    parser.add_argument(
        "--results",
        metavar="DIR",
        help="result JSON directory (default: <repo>/ci-results)",
    )
    parser.add_argument(
        "--charts",
        metavar="DIR",
        help="reference charts directory (default: <repo>/charts)",
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="output directory (default: <repo>/site/dist)",
    )
    parser.add_argument(
        "--lang",
        choices=("all", "en", "ru"),
        default="all",
        help="page language to build (default: all)",
    )
    args = parser.parse_args(argv)

    results_dir = Path(args.results).expanduser() if args.results else repo_root / "ci-results"
    charts_dir = Path(args.charts).expanduser() if args.charts else repo_root / "charts"
    out_dir = Path(args.out).expanduser() if args.out else repo_root / "site" / "dist"

    out_resolved = out_dir.resolve()
    if out_resolved == Path(out_resolved.anchor) or out_resolved == repo_root.resolve():
        parser.error("refusing to write the dashboard over the filesystem root or the repository root")
    if out_resolved in (results_dir.resolve(), charts_dir.resolve()):
        parser.error("output directory must not be the results or charts directory")

    selected, groups, copied, scanned, ignored, invalid, written = build(
        results_dir, charts_dir, out_dir, args.lang
    )
    for path in written:
        print(f"wrote {path}")
    print(
        f"copied {len(copied['en'])} English and {len(copied['ru'])} Russian "
        "reference charts"
    )
    print(
        f"results: {len(selected)} file{'s' if len(selected) != 1 else ''} charted, "
        f"{len(groups)} scenario{'s' if len(groups) != 1 else ''}, "
        f"{scanned} JSON file{'s' if scanned != 1 else ''} scanned"
    )
    if ignored:
        print(f"ignored {len(ignored)} file(s) without throughput metrics")
    if invalid:
        print(f"skipped {len(invalid)} unreadable file(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
