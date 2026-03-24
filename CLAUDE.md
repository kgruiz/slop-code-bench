# SlopCodeBench Paper Repository

## CRITICAL — Data Integrity
- **NEVER filter checkpoints in `data/checkpoints.csv` based on `status` or `state`.** All rows (`ran`, `unknown`, `error`) have full metrics. Filtering silently drops data.
- Some runs have fewer than 93 checkpoints (agent crashed). For **correctness metrics**, treat missing as 0. For **quality metrics**, leave them absent.

## CRITICAL — Writing Style
These are the rules you are most likely to violate. Treat each as a strong default, not a suggestion.

**Em dashes** — Avoid entirely. Use commas, semicolons, periods, or restructure the sentence.

**LinkedIn/corporate voice** — Words like "notably", "robust", "comprehensive", "leveraging", "significant" (outside statistics), "key insight", "crucially", "importantly" should be rare to nonexistent. Write like a scientist, not a thought leader.

**Colons** — Do not use colons to introduce explanatory clauses. Restructure as a separate sentence. Counted lists are fine ("three patterns: X, Y, and Z").

**Parenthetical asides** — Avoid. Inline the content as a standalone clause or sentence.

**Statistical test results** — Do not carpet-bomb paragraphs with $\rho_s$ and p-values. Use them ONLY when the statistical relationship itself is the claim. Lead results with concrete numbers (percentages, counts, ranges), not test statistics.

## Paper Overview
- **Title**: SlopCodeBench: Measuring Code Erosion Under Iterative Specification Refinement
- **Venue**: NeurIPS 2026 (preprint)
- **Dual claim**: (1) Iterative evaluation is the correct frame for coding agents because real development is iterative, (2) verbosity and structural erosion can only be measured under iteration; their compounding nature is the phenomenon.
- **Three findings**: (1) verbosity signals predict downstream performance while structural erosion does not, because tests are blind to sloppiness, (2) high-complexity symbols grow across checkpoints even when pass rates are stable, (3) initial design decisions compound into high variance
- **Contributions**: the benchmark (20 problems, 93 checkpoints), two-dimensional quality metrics (verbosity + structural erosion), initial experimental findings

## Prompt Convention
Main results use **only** the `just-solve` prompt. The section on prompt differences includes all prompt variants.

## Key Terminology
- **Slop**: Low-quality high-volume LLM code (verbose comments, defensive coding, unrequested features, bloated control flow)
- **Checkpoint**: One step in a problem's evolution; tuple of (spec $x_i$, test suite $\tau_i$). Agent receives $x_i$ + prior solution $s_{i-1}$, produces $s_i$
- **Verbosity**: `{AST-Grep Flagged Lines ∪ Clone Lines} / LOC`
- **Structural erosion**: `mass.high_cc_pct`; percent of CC mass from functions with CC > 10, where `mass.cc = sum(complexity * sqrt(symbol_sloc))`
- **LOC**: Logical lines (excludes comments and blanks); used for normalization throughout
- **Trivial wrapper**: A function whose only statement returns the result of calling another function
- **Single-use function**: A function called exactly once in the entire codebase

Note: delta metrics (loc, ast_grep_violations, churn_ratio) are not precomputed; calculate them manually from checkpoint data when needed.

## LaTeX Conventions
- Citations: `\citet{}` for textual, `\citep{}` for parenthetical (natbib)
- Cross-refs: always use `\autoref{}`. No manual `Figure~\ref{}` or `Table~\ref{}`
- Label prefixes: `sec:`, `fig:`, `tab:`, `eq:`
- Code listings: `lstlisting` with captions; Python highlighting preconfigured
- Math: `\text{}` for named quantities (`\text{Verbosity}`, `\text{LOC}`, `\text{Erosion}`)
- Model names: tilde between name and version (GPT~5.2, Opus~4.5)

## Writing Style — Additional
- 50/50 active "we" / passive voice; editorial "we" for emphasis
- End sections with a **bolded summary sentence**
- No rhetorical questions, ever
- Vary paragraph openings (topic sentence, data-first, backward reference)
- Hedge with "we hypothesize" not "may/might"; use "suggests" or "consistent with"
- Null results stated directly: "X has no effect on Y" or "changes Y by <1%"
- Ranges: "15% to 30%" in prose (no en-dash); CI or ± for statistics
- Oxford comma always; inline lists with count prefix
- "our approach", "this work", or method name (not "the proposed method")
- MINIMAL em-dashes/colons/parentheticals.

## Scripts
Scripts assume CWD is repo root. Shared code lives in `src/scbench/`, not as cross-imported helpers inside `scripts/`.

- `scripts/pipeline/` — extraction and dataset builders
- `scripts/generation/` — table/macro/report generators
- `scripts/plotting/` — figure entrypoints
- `scripts/exploration/` — ablations, comparisons, sweeps

## Links
- Repo: https://github.com/SprocketLab/slop-code-bench/
- Website: https://www.scbench.ai
- Blog: https://gabeorlanski.github.io/posts/slop-code-bench/