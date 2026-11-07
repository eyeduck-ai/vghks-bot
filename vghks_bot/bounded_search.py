"""User regular expressions run in a disposable process, never in the server."""
from __future__ import annotations

import multiprocessing
import re
from threading import BoundedSemaphore

MAX_REGEX_CORPUS_BYTES = 16 * 1024 * 1024
REGEX_RECORD_OVERHEAD = 64
MAX_REGEX_SPANS = 100_000
_regex_slots = BoundedSemaphore(2)


def validate_corpus_size(size):
    if size > MAX_REGEX_CORPUS_BYTES:
        raise ValueError("regex 搜尋資料過大，請縮小檢閱範圍或改用文字搜尋。")


def _search(pipe, pattern, texts):
    try:
        expression = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        result = {}
        count = 0
        for index, text in enumerate(texts):
            spans = []
            for match in expression.finditer(text):
                spans.append([match.start(), match.end()])
                if len(spans) == 50:
                    break
            if spans:
                count += len(spans)
                if count > MAX_REGEX_SPANS:
                    pipe.send({"error": "regex 命中過多，請縮小範圍或加入更明確的條件。"})
                    return
                result[index] = spans
        pipe.send({"matches": result})
    except re.error as exc:
        pipe.send({"error": "regex 格式錯誤：" + str(exc)})
    finally:
        pipe.close()


def search(pattern, texts, timeout=2.0):
    if not isinstance(pattern, str) or len(pattern) > 500:
        raise ValueError("搜尋內容最多 500 字。")
    if not _regex_slots.acquire(blocking=False):
        raise ValueError("目前有其他 regex 搜尋正在執行，請稍後再試。")
    try:
        validate_corpus_size(sum(REGEX_RECORD_OVERHEAD + (len(text) if text.isascii() else len(text.encode("utf-8")))
                                 for text in texts))
        return _run(pattern, texts, timeout)
    finally:
        _regex_slots.release()


def _run(pattern, texts, timeout):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_search, args=(sender, pattern, texts), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            raise ValueError("regex 執行逾時，請縮小範圍或簡化條件。")
        value = receiver.recv()
        if "error" in value:
            raise ValueError(value["error"])
        return value["matches"]
    finally:
        receiver.close()
        sender.close()
        if process.pid:
            process.join(.1)
            if process.is_alive():
                process.terminate()
                process.join(1)
