import pytest
from context import compress_conversation, should_compress

def messages(n=8): return [{"role": "user", "content": "x" * 1000} for _ in range(n)]

def test_compression_trigger_logic():
    assert should_compress(messages(), 10)
    assert not should_compress([{"role": "user", "content": "short"}], 10)

@pytest.mark.asyncio
async def test_compressed_structure_is_valid():
    async def llm(_, __, ___="", ____=None): return "summary"
    source = [{"role":"system","content":"sys"},{"role":"user","content":"task"}] + [{"role":"assistant","content":"x"*500} for _ in range(6)]
    result = await compress_conversation(source, llm, "sys", "task", keep_recent=2)
    assert result[0]["role"] == "system" and result[1]["content"] == "task"
    assert result[2]["content"].startswith("Summary of earlier conversation:")
    assert all(set(m) == {"role", "content"} for m in result)

@pytest.mark.asyncio
async def test_summary_failure_degrades_to_original():
    async def llm(_, __, ___="", ____=None): raise RuntimeError("down")
    source = messages()
    assert await compress_conversation(source, llm, "sys", "task") is source

@pytest.mark.asyncio
async def test_summary_character_limit():
    async def llm(_, __, ___="", ____=None): return "s" * 5000
    result = await compress_conversation(messages(), llm, "sys", "task", keep_recent=2)
    assert len(result[2]["content"].split("Summary of earlier conversation:\n", 1)[1]) <= 2000
