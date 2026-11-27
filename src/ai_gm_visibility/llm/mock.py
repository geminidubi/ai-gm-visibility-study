"""A deliberately naive evidence echo, never a semantic evaluator or model proxy."""

import re


class MockModel:
    model_id = "mock-evidence-echo-v1"

    def __init__(self, mode="echo"):
        if mode not in {"echo", "refuse", "empty", "error"}:
            raise ValueError("Unknown mock mode")
        self.mode = mode

    def generate(self, request):
        if self.mode == "error":
            raise RuntimeError("Synthetic model failure")
        if self.mode == "empty":
            text = ""
        elif self.mode == "refuse":
            text = "無法提供任何資訊。"
        else:
            content = request["messages"][2]["content"].split("證據：\n", 1)[1]
            text = "\n".join(re.sub(r"^\[[^\]]+\] ", "", line) for line in content.splitlines())
        return {"response_text": text, "returned_model": self.model_id,
                "usage": None, "finish_reason": "stop", "synthetic": True}
