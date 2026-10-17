---
name: test-the-tests
description: Check that the test suite (especially AI-written tests) actually catches broken behaviour, using mutation testing. Use after generating tests with AI, before trusting a coverage number, or when the user asks "are these tests any good?".
---

# Test the tests

Coverage says a line *ran*. Mutation testing says a test *noticed* when it was wrong.

1. Run `python devops/scripts/mutation_check.py`. Each line is a deliberately broken security control.
   - `killed`: good, a test failed.
   - `SURVIVED`: no test notices this control is broken. Write a test for it.
   - `stale`: the code moved; update the mutant's `original` string.
2. For each survivor, delegate to `test-writer` with the mutant description. The new test must fail
   on the mutant and pass on real code. Re-run to confirm `killed`.
3. Adding a new security control? Add a mutant for it in `MUTANTS`, in the same PR.
4. For broad, automatic mutants on non-security code: `pip install mutmut && mutmut run`.
   Review survivors and don't chase 100%. Focus on logic that matters.

Smells of weak AI-generated tests to look for while you're here:
asserting only "no exception", asserting on mocks instead of results, re-implementing the code under test
inside the test, snapshot tests nobody reads, and tests that pass with the function body deleted.
