from spec_cli.git import repo_name_from_remote_url


def test_repo_name_from_remote_url_github_https():
    assert (
        repo_name_from_remote_url("https://github.com/acme/billing-service.git")
        == "billing-service"
    )


def test_repo_name_from_remote_url_ssh_shorthand():
    assert repo_name_from_remote_url("git@github.com:acme/widget.git") == "widget"


def test_repo_name_from_remote_url_none():
    assert repo_name_from_remote_url(None) is None
    assert repo_name_from_remote_url("") is None
