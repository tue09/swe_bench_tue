from swebench.harness.log_parsers.c import MAP_REPO_TO_PARSER_C
from swebench.harness.log_parsers.go import MAP_REPO_TO_PARSER_GO
from swebench.harness.log_parsers.java import MAP_REPO_TO_PARSER_JAVA
from swebench.harness.log_parsers.javascript import MAP_REPO_TO_PARSER_JS
from swebench.harness.log_parsers.php import MAP_REPO_TO_PARSER_PHP
from swebench.harness.log_parsers.python import MAP_REPO_TO_PARSER_PY
from swebench.harness.log_parsers.ruby import MAP_REPO_TO_PARSER_RUBY
from swebench.harness.log_parsers.rust import MAP_REPO_TO_PARSER_RUST
from swebench.harness.log_parsers.python_swegym import MAP_REPO_TO_PARSER as MAP_REPO_TO_PARSER_SWEGYM

from collections import defaultdict
from swebench.harness.log_parsers.python import parse_log_pytest_v2

MAP_REPO_TO_PARSER = defaultdict(lambda: parse_log_pytest_v2)
MAP_REPO_TO_PARSER.update({
    **MAP_REPO_TO_PARSER_C,
    **MAP_REPO_TO_PARSER_GO,
    **MAP_REPO_TO_PARSER_JAVA,
    **MAP_REPO_TO_PARSER_JS,
    **MAP_REPO_TO_PARSER_PHP,
    **MAP_REPO_TO_PARSER_PY,
    **MAP_REPO_TO_PARSER_RUST,
    **MAP_REPO_TO_PARSER_RUBY,
    **MAP_REPO_TO_PARSER_SWEGYM,
})


__all__ = [
    "MAP_REPO_TO_PARSER",
]