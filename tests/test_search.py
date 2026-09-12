from beherouter.search import ToolIndex


def test_search_ranks_relevant_first():
    idx = ToolIndex()
    idx.add("behelib_search", "Ranked semantic and graph search over indexed knowledge", [])
    idx.add("behelib_shelf_create", "Create a new shelf to hold boxes", [])
    idx.add("behelib_health", "Report service health and readiness", [])
    hits = idx.search("search knowledge", limit=2)
    assert hits[0] == "behelib_search"
    assert len(hits) <= 2


def test_search_empty_index_returns_empty():
    assert ToolIndex().search("anything") == []


def test_search_finds_long_tail_by_summary():
    idx = ToolIndex()
    idx.add("behelib_search", "Ranked search over knowledge", [])
    idx.add("behelib_shelf_create", "Create a new shelf to hold boxes", [])
    assert "behelib_shelf_create" in idx.search("create shelf")


def test_search_matches_on_name_tokens():
    """Flat tool names are tokenized too — `shelf_create` is searchable as `shelf`."""
    idx = ToolIndex()
    idx.add("behelib_shelf_create", "no useful words here", [])
    assert "behelib_shelf_create" in idx.search("shelf")


def test_add_after_search_reindexes():
    """The index is lazily built; adding after a search must invalidate it."""
    idx = ToolIndex()
    idx.add("a_one", "alpha", [])
    assert idx.search("beta") == []
    idx.add("a_two", "beta", [])
    assert idx.search("beta") == ["a_two"]


def test_zero_score_hits_are_excluded():
    idx = ToolIndex()
    idx.add("a_one", "alpha", [])
    assert idx.search("completelyunrelatedterm") == []


def test_term_present_in_half_the_corpus_is_still_found():
    """Regression: BM25Okapi IDF is exactly 0 for a term in half the documents.

    Filtering hits on `score > 0` (the original plan) silently dropped these.
    On a ~100-tool Gitea surface that means "repo" — in ~half the tools —
    would return nothing, defeating the whole point of the search meta-tool.
    """
    idx = ToolIndex()
    for i in range(50):
        idx.add(f"gitea_repo_op_{i}", "operate on a repo", [])
    for i in range(50):
        idx.add(f"gitea_user_op_{i}", "operate on a user", [])
    hits = idx.search("repo", limit=100)
    assert len(hits) == 50
    assert all("repo" in h for h in hits)


def test_single_tool_surface_is_searchable():
    """Regression: a 1-document corpus gives BM25 a negative IDF for every term."""
    idx = ToolIndex()
    idx.add("solo_only_tool", "the only tool here", [])
    assert idx.search("only") == ["solo_only_tool"]


def test_keywords_are_searchable():
    idx = ToolIndex()
    idx.add("a_one", "no match in summary", ["conformance"])
    assert idx.search("conformance") == ["a_one"]


def test_search_tolerates_a_typo():
    idx = ToolIndex()
    idx.add("t_convert", "convert a document", [])
    idx.add("t_delete", "remove a file", [])
    assert "t_convert" in idx.search("converrt")


def test_search_matches_a_morphological_variant():
    idx = ToolIndex()
    idx.add("t_repository_list", "list repositories", [])
    assert "t_repository_list" in idx.search("repo")


def test_exact_matches_outrank_fuzzy_ones():
    idx = ToolIndex()
    idx.add("t_convert", "convert a document", [])
    idx.add("t_converge", "converge the thing", [])
    hits = idx.search("convert")
    assert hits[0] == "t_convert"


def test_fuzzy_does_not_fire_when_exact_fills_the_limit():
    """Precision guard: a good exact result set must not be diluted."""
    idx = ToolIndex()
    for i in range(5):
        idx.add(f"t_search_{i}", "search things", [])
    idx.add("t_seaarch_typo", "unrelated", [])
    assert idx.search("search", limit=5) == [f"t_search_{i}" for i in range(5)]


def test_short_tokens_do_not_prefix_match_everything():
    idx = ToolIndex()
    idx.add("t_a", "alpha beta", [])
    idx.add("t_b", "gamma delta", [])
    assert idx.search("xy") == []
