"""User regular expressions run in a disposable process, never in the server."""
from __future__ import annotations

import multiprocessing
import re


def _search(pipe, pattern, texts):
    try:
        expression = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        result = {}
        for index, text in enumerate(texts):
            spans = []
            for match in expression.finditer(text):
                spans.append([match.start(), match.end()])
                if len(spans) == 50:
                    break
            if spans:
                result[index] = spans
        pipe.send({"matches": result})
    except re.error as exc:
        pipe.send({"error": "regex 格式錯誤：" + str(exc)})
    finally:
        pipe.close()


def search(pattern, texts, timeout=2.0):
    if not isinstance(pattern, str) or len(pattern) > 500:
        raise ValueError("搜尋內容最多 500 字。")
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
