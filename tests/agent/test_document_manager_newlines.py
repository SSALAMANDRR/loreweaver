from agent.document_manager import DocumentProcessor


def test_txt_extraction_normalizes_crlf_and_bare_cr():
    source = b"first\r\nsecond\rthird\n"

    assert DocumentProcessor.extract_text_from_txt(source) == "first\nsecond\nthird\n"
