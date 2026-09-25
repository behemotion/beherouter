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


from beherouter.search import DEFAULT_LIMIT, normalize, singular, split_lead


def test_normalize_splits_camel_case_and_snake_case():
    assert normalize("projectId work_item") == ["project", "id", "work", "item"]


def test_normalize_drops_stopwords():
    assert normalize("change the status of a ticket to done") == [
        "change", "status", "ticket", "done",
    ]


def test_singular_folds_plurals():
    assert singular("issues") == "issue"
    assert singular("repositories") == "repository"
    assert singular("boxes") == "box"
    assert singular("classes") == "class"
    assert singular("workitems") == "workitem"


def test_singular_leaves_non_plurals_alone():
    for word in ("status", "access", "alias", "analysis", "bus", "item"):
        assert singular(word) == word


def test_split_lead_takes_the_first_sentence():
    assert split_lead("Comments on a work item. Actions: list, create.") == (
        "Comments on a work item.",
        "Actions: list, create.",
    )
    assert split_lead("No full stop\nsecond line") == ("No full stop", "second line")


def test_filler_words_do_not_decide_membership():
    """Regression: 'to'/'of'/'a' used to put most of the corpus in tier 0."""
    idx = ToolIndex()
    idx.add("state", "Workflow states within a project.", [])
    for i in range(12):
        idx.add(f"other_{i}", "Something to do with a thing of the workspace.", [])
    assert idx.search("change status of a ticket to done") == []
    idx2 = ToolIndex()
    idx2.add("state", "Workflow states within a project.", [], ("status",))
    for i in range(12):
        idx2.add(f"other_{i}", "Something to do with a thing of the workspace.", [])
    assert idx2.search("change status of a ticket to done") == ["state"]


def test_plural_query_finds_singular_name():
    idx = ToolIndex()
    idx.add("workitem", "Work items -- issues, tasks and epics.", [])
    for i in range(8):
        idx.add(f"workitem_x{i}", "Things on workitems.", [])
    assert idx.search("workitems")[0] == "workitem"


def test_exact_name_match_ranks_first():
    idx = ToolIndex()
    idx.add("work_log", "Time logged against a work item.", [])
    idx.add("workitem", "Work items. Work work work item item.", [])
    assert idx.search("work log")[0] == "work_log"


def test_aliases_are_searchable():
    idx = ToolIndex()
    idx.add("cycle", "Cycles (time-boxed iterations) in a project.", [], ("sprint",))
    idx.add("module", "Modules in a project.", [])
    assert idx.search("sprint") == ["cycle"]


def test_weak_hits_are_cut_off():
    idx = ToolIndex()
    idx.add("attachment", "Files attached to a work item.", ["upload"])
    for i in range(6):
        idx.add(f"t_{i}", f"Unrelated tool number {i} mentioning a file once.", [])
    hits = idx.search("upload attachment")
    assert hits == ["attachment"]


def test_default_limit_is_five():
    idx = ToolIndex()
    for i in range(9):
        idx.add(f"t_{i}", "search things", [])
    assert DEFAULT_LIMIT == 5
    assert len(idx.search("search")) == 5
