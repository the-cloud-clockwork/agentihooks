import new_ledger

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show
from tests.swarm_ledger.test_comment_author_lines import browser as browser

SLOW_FIRST_READ = """
const pipe = ReadableStream.prototype.pipeThrough;
ReadableStream.prototype.pipeThrough = function (...args) {
  const out = pipe.apply(this, args);
  const getReader = out.getReader.bind(out);
  out.getReader = () => {
    const reader = getReader();
    const read = reader.read.bind(reader);
    let first = true;
    reader.read = () => {
      if (!first) return read();
      first = false;
      return new Promise((done) => setTimeout(() => done(read()), 800));
    };
    return reader;
  };
  return out;
};
"""


def test_the_shared_wait_returns_only_after_the_first_ledger_state_renders(browser):
    doc = new_ledger.build_doc({"title": "Wait", "overview": "o", "phases": [{"title": "one"}, {"title": "two"}]})
    with browser.new_context() as context:
        context.add_init_script(SLOW_FIRST_READ)
        tab = context.new_page()
        show(tab, shell_html(), ledger=ledger_state(doc))
        assert tab.locator("#phases > li").count() == 2
