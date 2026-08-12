import unittest

from transform.feishu_doc import _INLINE_MATH_RE, _inline_math_latex
from transform.feishu_latex_compat import validate_feishu_latex
from transform.katex_validate import extract_math


class LatexValidationTests(unittest.TestCase):
    def test_extracts_bracket_display_and_paren_inline_math(self):
        markdown = r"""
\[
\frac{\ppl_{m_i}(t \mid c)}{\ppl_{m_j}(t \mid c)}
= \alpha + \beta \cdot (i-j).
\]

Inline \(x_i \lt i\) should also be validated.
"""

        expressions = extract_math(markdown)

        self.assertEqual(len(expressions), 2)
        self.assertTrue(expressions[0]["display"])
        self.assertIn(r"\ppl_{m_i}", expressions[0]["latex"])
        self.assertFalse(expressions[1]["display"])
        self.assertEqual(expressions[1]["latex"], r"x_i \lt i")

    def test_feishu_compat_flags_known_unsupported_commands(self):
        markdown = r"""
$$
p(x_i \mid x_{\lt i}) =
\frac{\mathbbm{1}(p'_i \gt 0) \cdot p'_i}
{\sum \mathbbm{1}(p'_i \gt 0)\cdot p'_i}.
$$

\[
\frac{\ppl_{m_i}(t \mid c)}{\ppl_{m_j}(t \mid c)}
= \alpha + \beta \cdot (i-j).
\]
"""

        errors = validate_feishu_latex(markdown)

        self.assertEqual({error["command"] for error in errors}, {r"\mathbbm", r"\ppl"})

    def test_inline_math_regex_accepts_dollar_and_paren_delimiters(self):
        text = r"Use $x_i$ and \(y_i\)."

        matches = list(_INLINE_MATH_RE.finditer(text))

        self.assertEqual([_inline_math_latex(match) for match in matches], ["x_i", "y_i"])


if __name__ == "__main__":
    unittest.main()
