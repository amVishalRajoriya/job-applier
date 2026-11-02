"""A job title like 'Lead Security Engineer' names the role; it is not a claim about the candidate."""
from pathlib import Path

from jobagent import profile as P
from jobagent.truth_guard import check_free_text

ROOT = Path(__file__).resolve().parents[1]


def test_job_title_in_letter_is_not_a_claim():
    prof = P.load_yaml(str(ROOT / "data/sample/profile_SYNTHETIC.yaml"))
    letter = "I am applying for the Lead Security Engineer role at Razorpay."
    assert check_free_text(letter, prof, quoted=("Lead Security Engineer", "Razorpay")) == []


def test_same_words_outside_the_title_are_still_checked():
    prof = P.load_yaml(str(ROOT / "data/sample/profile_SYNTHETIC.yaml"))
    letter = "I am applying for the Lead Security Engineer role at Razorpay. I lead the security team."
    problems = check_free_text(letter, prof, quoted=("Lead Security Engineer", "Razorpay"))
    assert problems
