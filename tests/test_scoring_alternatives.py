"""'Proficiency in any one of PHP, Python or Java' is satisfied by Python alone."""
from pathlib import Path

from jobagent import profile as P
from jobagent.requirements import parse_job
from jobagent.resume_doc import master_resume
from jobagent.scoring import alternative_groups, score

ROOT = Path(__file__).resolve().parents[1]


def _score(jd: str):
    prof = P.load_yaml(str(ROOT / "data/sample/profile_SYNTHETIC.yaml"))
    reqs = parse_job(jd)
    return reqs, score(master_resume(prof), prof, reqs, "Backend Engineer")


def test_any_one_of_counts_the_best_option():
    reqs, res = _score("Requirements\n- Proficiency in any one of PHP, Python, Golang, Java or C++\n- Good understanding of REST APIs")
    assert alternative_groups(reqs)
    assert not [m for m in res.missing_requirements if "PHP" in m or "C++" in m]


def test_and_list_is_still_all_required():
    reqs, res = _score("Requirements\n- Hands-on experience with Golang and Rust\n- Good understanding of REST APIs")
    assert not alternative_groups(reqs)
    assert any("Rust" in m or "Go" in m for m in res.missing_requirements)


def test_preferred_masters_does_not_raise_required_degree():
    reqs = parse_job("Requirements\n- Engineering degree in Computer Science or a related field\n"
                     "Nice to have\n- Master's degree")
    assert reqs.degree_level == 3 and reqs.degree_required


def test_bachelors_or_masters_means_bachelors():
    reqs = parse_job("Requirements\n- Bachelor's or Master's degree in Computer Science or IT")
    assert reqs.degree_level == 3
