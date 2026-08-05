from app.agents.tools.link_extractor import extract_candidate_links


def test_extracts_google_meet_link():
    text = "Join us at https://meet.google.com/abc-defg-hij for the sync."
    links = extract_candidate_links(text)
    assert any("meet.google.com" in l for l in links)


def test_no_links_returns_empty():
    assert extract_candidate_links("Just a regular email, no links here.") == []


def test_dedupes_repeated_links():
    link = "https://zoom.us/j/1234567890"
    text = f"{link} see you there. Again: {link}"
    links = extract_candidate_links(text)
    assert links.count(link) == 1
