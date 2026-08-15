from engram.tokenizer import fts_document, tokenize


def test_chinese_is_split_into_chars_and_bigrams() -> None:
    tokens = tokenize("认知工效")
    assert "认" in tokens
    assert "认知" in tokens
    assert "知工" in tokens


def test_compound_identifier_keeps_whole_and_parts() -> None:
    tokens = tokenize("item_fille/招聘数据收集/README.md")
    assert "item_fille" in tokens
    assert "readme.md" in tokens


def test_slash_identifier_is_split_and_preserved() -> None:
    tokens = tokenize("browser-use/video-use")
    assert "browser-use/video-use" in tokens
    assert "browser-use" in tokens
    assert "video-use" in tokens


def test_tokenize_is_case_insensitive() -> None:
    assert tokenize("MiniMax") == tokenize("minimax")


def test_fts_document_is_space_joined() -> None:
    document = fts_document("AI 架构")
    assert " " in document
    assert "架构" in document
