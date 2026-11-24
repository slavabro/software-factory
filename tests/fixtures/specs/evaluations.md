---
capability: evaluations
owners: [common/evaluations, common/policies]
---
# Asset evaluations

Each asset the office tracks is rated on the criteria that fit what it is; rule criteria are rated from facts,
judged criteria by an analyst desk or the principal.

## rule-ratings-follow-facts
The office SHALL rate every tracked asset on each rule criterion that applies to its kind, and SHALL rewrite a rule
rating when the facts behind it move.

Proven by:
- tests/database/test_rule_pass.py::test_rates_holds_and_yields
- tests/database/test_rule_pass.py::test_a_rule_rating_rewrites_when_its_facts_move_and_its_words_do_not

## judgement-is-never-pruned
Disabling a criterion SHALL remove only the ratings a rule gave; a rating the principal or an analyst gave SHALL be
kept.

Proven by:
- tests/database/test_rule_pass.py::test_disabled_criterion_prunes_rule_ratings_only

## no-rating-on-a-missing-fact
When a fact a rule needs is missing, the office SHALL drop that rule rating and count it as missing rather than rate
on a guess, and SHALL name an asset whose kind it does not know.

Proven by:
- tests/database/test_rule_pass.py::test_cleared_fact_prunes_and_counts_missing_and_a_skip_does_not
- tests/database/test_rule_pass.py::test_unknown_kind_is_named

## review-only-on-change
An analyst review SHALL start only when a rule pass changed a rating, and a review that fails to start SHALL NOT
fail the rule pass.

Proven by:
- tests/temporal/test_evaluation_rules_workflow.py::test_a_pass_that_changed_a_rating_starts_the_review
- tests/temporal/test_evaluation_rules_workflow.py::test_a_pass_that_changed_nothing_starts_no_review
- tests/temporal/test_evaluation_rules_workflow.py::test_a_failed_start_never_fails_the_rule_pass

## desks-change-criteria-by-decision
A desk SHALL change an evaluation criterion only through an approved decision on its own department's criteria; a
change to another department's criterion, or one the principal overtook after the proposal, SHALL be refused and
write nothing.

Proven by:
- tests/database/test_policy_change_decision.py::test_methodology_decision_applies_through_store
- tests/database/test_policy_change_decision.py::test_decision_wrong_lane_fails_action
- tests/database/test_policy_change_decision.py::test_draft_stale_at_save_writes_nothing

## no-criterion-rates-where-to-buy
No criterion SHALL rate where an asset can be bought; that is the approved list's question.

Proven by:
- tests/standalone/test_evaluation_criteria_files.py::test_nothing_rates_where_it_can_be_bought
