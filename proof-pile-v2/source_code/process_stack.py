import argparse
from datasets import load_dataset
from itertools import islice
from tqdm import tqdm
import os
import json
import ndjson
import sys
import random
import re
from pathlib import Path
import shutil
import nbformat
from functools import reduce, partial
import tiktoken
from nbconvert.exporters import MarkdownExporter

import code

"""
Just as a reminder, here are the stack keys:

hexsha
size
ext
lang
max_stars_repo_path
max_stars_repo_name
max_stars_repo_head_hexsha
max_stars_repo_licenses
max_stars_count
max_stars_repo_stars_event_min_datetime
max_stars_repo_stars_event_max_datetime
max_issues_repo_path
max_issues_repo_name
max_issues_repo_head_hexsha
max_issues_repo_licenses
max_issues_count
max_issues_repo_issues_event_min_datetime
max_issues_repo_issues_event_max_datetime
max_forks_repo_path
max_forks_repo_name
max_forks_repo_head_hexsha
max_forks_repo_licenses
max_forks_count
max_forks_repo_forks_event_min_datetime
max_forks_repo_forks_event_max_datetime
content
avg_line_length
max_line_length
alphanum_fraction
"""

SAVE_BATCH_SIZE = 50_000
EVAL_RATIO=0.005
DATA_DIR = "data_jsonl"
META_DIR = "meta_json"

TEXT_MAX_SIZE = 1048575 # in bytes
MAX_NUMERICAL_DENSITY = .5

DATA_DIRS = [
    # numerical/statistical computing
    "r",
    # CAS
    "maple",
    "gap",
    # formal math
    "lean",
    "isabelle",
    "idris", 
    "agda",
    # imperative languages
    "python",
    "jupyter-notebook",
    "julia",
    "c",
    "cpp",
    # markup languages
    "tex",
]



def numerical_density(ex):
    # The ratio of digit non-whitespace characters over non-digit non-whitespace
    # characters in the file
    txt = ''.join(ex["content"].split())
    ntoks = sum(txt.count(c) for c in "0123456789")
    return ntoks / len(txt)

def standard_filter(
        example, 
        max_numerical_density=MAX_NUMERICAL_DENSITY, 
        text_max_size=TEXT_MAX_SIZE
):
    """
    Byte length and numerical density filter that is repeated throughout
    this script
    """
    if len(example["content"].encode("utf-8")) > text_max_size:
        return False
    elif numerical_density(example) > max_numerical_density: 
        return False
    else: 
        return True


is_reference_design_rexp = re.compile(r"Requirement\s+\{\s+Identifier")
def r_filter(example):
    if not standard_filter(example): 
        return False

    is_resource_fork = "/* Resource fork" in example["content"]
    if is_resource_fork:
        return False

    if is_reference_design_rexp.search(example["content"]):
        return False

    is_xml = example["content"].startswith("<?xml")
    if is_xml:
        return False

    # R files are not supposed to be notebooks
    is_notebook = example["content"].startswith("{")
    if is_notebook:
        return False

    return True


def maple_filter(example):
    if not standard_filter(example, text_max_size=100_000): 
        return False

    return "<?xml" != example["content"][:5]


def gap_filter(example): 
    return standard_filter(example)

def lean_filter(example): 
    return standard_filter(example)

def isabelle_filter(example): 
    return standard_filter(example)

def idris_filter(example): 
    return standard_filter(example)

def agda_filter(example): 
    return standard_filter(example)

def py_filter(example):
    text = example["content"]
    
    if not standard_filter(example): 
        return False
    
    # removes notebooks and jsons
    if text.strip()[0] == "{": 
        return False

    keywords = []
    packages = [
        "numpy",
        "scipy",
        "sympy",
        "sage",
        "numba",
        "numexpr",
    ]
    for pack in packages:
        keywords += [f"import {pack}", f"from {pack}"]

    found = [x for x in keywords if x in text]
    return found


def c_filter(example):
    if not standard_filter(example):
        return False

    text = example["content"]
    keywords = [
        "#include <fftw.h>",
        "#include <fftw3.h>"
        "#include <rfftw.h>",
        "#include <gsl",
        "#include <cblas.h>",
        "#include <blas.h>",
        "#include <lapacke.h>",
        "#include <nlopt.h>",
        "#include <petsc.h>"
    ]

    found = [x for x in keywords if x in text]
    return found


def cpp_filter(example):
    if not standard_filter(example, max_numerical_density=0.1): 
        return False

    text = example["content"]
    keywords = [
        "#include <adept_arrays.h>",
        "#include <adept.h>",
        "#include <alglib",
        "#include <boost",
        "#include <armadillo",
        "#include <blitz",
        "#include <Eigen",
        "#include <deal.II",
        "#include <dlib",
        "#include <NTL",
        "#include <mtl",
    ]

    found = [x for x in keywords if x in text]
    return found


def julia_test_file(ex, ratio=0.1):
    # Whether a file has some minimum ratio of @test statements
    txt = ex["content"]
    kwd = "@test"
    nlines = txt.count("\n") + 1
    return kwd in txt and (txt.count(kwd) / nlines >= ratio)


def generated_file(ex):
    # This heuristic happens to be superfluous
    return (
        "generated" in ex["max_stars_repo_name"] or ex["max_stars_repo_name"][0] == "."
    )


def julia_filter(ex):
    if ex["content"][0] in ["%", "{", "["]:
        # Eliminates non-Julia files such as JSON lines (.jl) files
        return False
    elif ex["size"] >= 1e5:
        # Overly large files are often auto-generated boilerplate and/or mostly
        # contain large arrays of numbers.Thus, we reject such large files unless
        # unless they are test files with low numerical density.
        return julia_test_file(ex) and numerical_density(ex) <= 0.5
    else:
        return True


# A list of keywords that make a Julia file interesting
julia_whitelist = [
    # Popular packages for scientific computing
    "LinearAlgebra",
    "DifferentialEquations",
    "Symbolics",
    "Distributions",
    "DataFrames",
    "DynamicalSystems",
    "Turing",
    "Gen",
    "JuMP",
    # Standard mathematical functions
    "sqrt",
    "abs",
    "zeros",
    "ones",
    "sin",
    "cos",
    "tan",
    "log",
    "exp",
    "integrate",
    "likelihood",
    "Matrix",
    "π",
    "pi",
    "rand",
    "grad",
]

julia_whitelist_rexp = re.compile(
    "|".join("(\\W" + kwd + "\\W)" for kwd in julia_whitelist)
)


def julia_filter_strict(ex):
    # A stricter Julia filter that operates from a whitelist
    return julia_filter(ex) and julia_whitelist_rexp.search(ex["content"])


def tex_filter_rexp(example, rexp):
    if not standard_filter(example, text_max_size=10_000_000): 
        return False

    if example["ext"] != "tex":
        return False

    if "latex/" in example["max_stars_repo_path"]:
        return False

    text = example["content"]

    if rexp.search(text):
        return False

    if "gnuplot" in text:
        return False

    keywords = [
        "\\chapter{",
        "\\chapter*{",
        "\\section{",
        "\\section*{",
        "\\subsection{",
        "\\subsection*{",
        "\\subsubsection{",
        "\\subsubsection*{",
        "\\paragraph{",
        "\\subparagraph{",
    ]

    found = [x for x in keywords if x in text]
    return bool(found)


h = re.compile("[\u0370-\u18aA\u3000-\U0001047f]")
tex_filter = partial(tex_filter_rexp, rexp=h)


def jupyter_notebook_filter(example):
    """
    We don't apply the TEXT_MAX_SIZE filter to jupyter notebooks, as of yet
    """
    text = example["content"]
    lower = text.lower()
    keywords = {"\\begin{equation}", "\\begin{align}", "import sympy", "from sympy"}
    for keyword in keywords:
        if keyword in lower:
            return True
    return False


def _filter_cell_output(output):
    # See https://ipython.org/ipython-doc/3/notebook/nbformat.html
    #   as a reference on formatting.
    # Remove image/png data (a base64 string).
    if (
        "output_type" in output
        and "data" in output
        and "image/png" in output["data"]
        and len(output["data"]["image/png"]) > 0
    ):
        return True

    # Remove exceptions.
    if "ename" in output and "traceback" in output:
        return True
    return False

# regular expression from https://stackoverflow.com/questions/44227270/regex-to-parse-image-link-in-markdown
markdown_image_rexp = re.compile(r'!\[[^\]]*\]\((?P<filename>.*?)(?=\"|\))(?P<optionalpart>\".*\")?\)')
html_image_rexp = re.compile(r'<img[^>]*>')
html_other_rexp = re.compile(r'<video.*?</video>|<script.*?</script>|<iframe.*?</iframe>', re.DOTALL)

def process_jupyter_notebook(example):
    try:
        content = example["content"]
        notebook = nbformat.reads(content, as_version=4)

        # Filter output content.
        for cell in notebook.cells:
            if "outputs" in cell:
                clear = False
                for output in cell["outputs"]:
                    if _filter_cell_output(output):
                        clear = True
                        break
                if clear:
                    cell["outputs"] = []

        # Convert to Markdown
        exporter = MarkdownExporter()
        body, resources = exporter.from_notebook_node(notebook)

        # remove markdown images 
        body = re.sub(markdown_image_rexp, '', body)
        # remove unwanted html elements
        body = re.sub(html_image_rexp, '', body)
        body = re.sub(html_other_rexp, '', body)

        example["content"] = body
        example["converted"] = True

    # Mark to discard later if conversion wasn't successful.
    except Exception:
        example["converted"] = False
    return example


def filter_processed_jupyter_notebook(example):
    return example["converted"]


def token_length(examples):
    tokenizer = tiktoken.get_encoding("cl100k_base")
    return {
        "num_tokens": [
            len(x)
            for x in tokenizer.encode_batch(examples["content"], disallowed_special=())
        ]
    }


def batch_loader(ds, size):
    """
    Iterator that takes in a list `seq` and returns
    chunks of size `size`"""
    for pos in range(0, len(ds), size):
        if pos + size < len(ds):
            yield ds.select(list(range(pos, pos + size)))
        else:
            yield ds.select(list(range(pos, len(ds))))


def save_dict_of_example(x):
    return {"text": x["content"], "meta": {k: x[k] for k in x if k != "content"}}


def main(args):
    NUM_PROC = args.cpus
    if "all" in args.langs:
        data_dirs = DATA_DIRS
    else:
        data_dirs = args.langs

    stats = {}

    for lang in data_dirs:
        print(lang.upper() + "#" * 70)
        use_auth_token = None
        if (tok := os.environ.get("HUGGING_FACE_TOKEN")) is not None:
            use_auth_token = tok
        print(f"loading {lang} data...")
        ds = load_dataset(
            "bigcode/the-stack-dedup",
            data_dir=f"data/{lang}",
            split="train",
            use_auth_token=use_auth_token,
        )

        print("filtering dataset...")
        filter_kwargs = {"num_proc": NUM_PROC, "load_from_cache_file": False}
        if lang == "r":
            ds = ds.filter(r_filter, **filter_kwargs)
        elif lang == "maple":
            ds = ds.filter(maple_filter, **filter_kwargs)
        elif lang == "gap": 
            ds = ds.filter(gap_filter, **filter_kwargs)
        elif lang == "lean": 
            ds = ds.filter(lean_filter, **filter_kwargs)
        elif lang == "idris": 
            ds = ds.filter(idris_filter, **filter_kwargs)
        elif lang == "agda": 
            ds = ds.filter(agda_filter, **filter_kwargs)
        elif lang == "isabelle": 
            ds = ds.filter(isabelle_filter, **filter_kwargs)
        elif lang == "python":
            ds = ds.filter(py_filter, **filter_kwargs)
        elif lang == "c":
            ds = ds.filter(c_filter, **filter_kwargs)
        elif lang == "cpp":
            ds = ds.filter(cpp_filter, **filter_kwargs)
        elif lang == "tex":
            ds = ds.filter(tex_filter, **filter_kwargs)
        elif lang == "julia":
            ds = ds.filter(julia_filter, **filter_kwargs)
        elif lang == "jupyter-notebook":
            ds = ds.filter(jupyter_notebook_filter, **filter_kwargs)
            ds = ds.map(process_jupyter_notebook, **filter_kwargs)
            ds = ds.filter(
                filter_processed_jupyter_notebook,
                **filter_kwargs,
            )
        else:
            print("NO FILTERING APPLICABLE")

        print("calculating tokens...")

        ds = ds.map(
            token_length,
            batched=True,
            batch_size=1000,
            num_proc=NUM_PROC,
            load_from_cache_file=False,
        )
        print("DONE CALCULATING TOKENS")

        for x in islice(ds, 1):
            print(x["content"])

        # counts number of files and dataset byte size and tokens in single loop
        print("calculating dataset statistics...")
        files, size, tokens = reduce(
            lambda x, y: (x[0] + 1, x[1] + y["size"], x[2] + y["num_tokens"]),
            tqdm(ds),
            (0, 0, 0),
        )

        stats_of_lang = {"files": files, "size": size, "num_tokens": tokens}

        print("printing stats...")
        print(stats_of_lang)

        print("saving dataset to disk in batches...")

        Path(os.path.join(DATA_DIR, "train/")).mkdir(parents=True, exist_ok=True)
        Path(os.path.join(DATA_DIR, "validation/")).mkdir(parents=True, exist_ok=True)
        Path(os.path.join(DATA_DIR, "test/")).mkdir(parents=True, exist_ok=True)
        Path(META_DIR).mkdir(exist_ok=True)

        # train, validation, test, spit
        test_len = max(int(EVAL_RATIO * len(ds)), 1)
        # shuffle, just in case there's some ordering to upstream data. 
        # note this slows down the script, since we're not accessing contiguous memory.
        ds = ds.shuffle(seed=74)
        train = ds.select(range(len(ds) - 2 * test_len))
        validation = ds.select(range(len(ds) - 2 * test_len, len(ds) - test_len))
        test = ds.select(range(len(ds) - test_len, len(ds)))

        print(f"TRAIN LENGTH: {len(train)}")
        print(f"VALIDATION LENGTH: {len(validation)}")
        print(f"TEST LENGTH: {len(test)}")

        # save train, valid, test
        print("saving dataset to disk...")
        num_batches = len(ds) // SAVE_BATCH_SIZE + 1
        digits_in_filename = max(len(str(num_batches)), 4)
        for i, batch in tqdm(
            enumerate(batch_loader(train, SAVE_BATCH_SIZE)),
            total=num_batches,
        ):
            with open(
                os.path.join(
                    DATA_DIR,
                    "train",
                    lang + str(i).zfill(digits_in_filename) + ".jsonl",
                ),
                "w",
            ) as f:
                for x in batch:
                    f.write(json.dumps(save_dict_of_example(x)))
                    f.write("\n")

        with open(
            os.path.join(DATA_DIR, "validation", f"{lang}-validation.jsonl"), "w"
        ) as f:
            for x in validation:
                f.write(json.dumps(save_dict_of_example(x)) + "\n")

        with open(os.path.join(DATA_DIR, "test", f"{lang}-test.jsonl"), "w") as f:
            for x in test:
                f.write(json.dumps(save_dict_of_example(x)) + "\n")

        print("saving stats to disk...")
        stats_path = os.path.join(META_DIR, "stack-stats.json")
        if os.path.isfile(stats_path):
            with open(stats_path) as f:
                stats = json.load(f)
        else:
            stats = dict()

        stats[lang] = stats_of_lang
        with open(stats_path, "w") as f:
            f.write(json.dumps(stats, indent=2))

        # creating repo index
        print(f"creating {lang} repo index...")
        repo_index = list(set([x["max_stars_repo_name"] for x in tqdm(ds)]))
        repo_index_path = os.path.join(META_DIR, f"{lang}_index")
        with open(repo_index_path, "w") as f:
            f.write("\n".join(repo_index))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--cpus", type=int, required=True)
    parser.add_argument(
        "-l",
        "--langs",
        nargs="+",
        default="all",
        help="space separated list of languages, if empty defaults to all",
    )
    args = parser.parse_args()
    main(args)
