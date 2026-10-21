"""Keep OS-lock test artifacts out of the user's tool runtime directory."""
import tempfile
from unittest import mock

import pytest
import project_job_lock


@pytest.fixture(scope="session", autouse=True)
def isolated_project_lock_runtime():
    with tempfile.TemporaryDirectory(prefix="noveltool-test-locks-") as root:
        with mock.patch.object(project_job_lock, "GUARD_DIR", root):
            yield
