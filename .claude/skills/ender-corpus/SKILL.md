---
name: ender-corpus
description: Analyze Project Ender corpus labelling progress and action distributions. Use when George asks about corpus stats, label distribution, teacher comparison, or labelling batch progress.
allowed-tools: Bash, Read
---

Analyze the Project Ender corpus database at `/home/aldric/Project-Ender/src/corpus.db` and report on labelling progress and distribution quality.

Run these SQL queries against the SQLite database:

## 1. Overall progress

```sql
SELECT COUNT(*) as total_states FROM states;

SELECT teacher,
       COUNT(*) as labels,
       ROUND(AVG(confidence), 3) as avg_conf,
       ROUND(MIN(confidence), 3) as min_conf,
       ROUND(MAX(confidence), 3) as max_conf
FROM labels
GROUP BY teacher
ORDER BY labels DESC;
```

## 2. Action distribution per teacher (as % of that teacher's total)

```sql
SELECT action_id,
  ROUND(100.0 * SUM(CASE WHEN teacher LIKE '%gemma%' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN teacher LIKE '%gemma%' THEN 1 ELSE 0 END), 0), 1) as gemma_pct,
  ROUND(100.0 * SUM(CASE WHEN teacher = 'lmstudio_qwen3-14b' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN teacher = 'lmstudio_qwen3-14b' THEN 1 ELSE 0 END), 0), 1) as qwen3_pct,
  ROUND(100.0 * SUM(CASE WHEN teacher = 'lmstudio_gpt-oss-20b' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN teacher = 'lmstudio_gpt-oss-20b' THEN 1 ELSE 0 END), 0), 1) as gptoss_pct,
  ROUND(100.0 * SUM(CASE WHEN teacher = 'lmstudio_deepseek-r1-distill-qwen-14b' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN teacher = 'lmstudio_deepseek-r1-distill-qwen-14b' THEN 1 ELSE 0 END), 0), 1) as deepseek_pct,
  ROUND(100.0 * SUM(CASE WHEN teacher = 'claude' THEN 1 ELSE 0 END) /
    NULLIF(SUM(CASE WHEN teacher = 'claude' THEN 1 ELSE 0 END), 0), 1) as claude_pct
FROM labels
GROUP BY action_id
ORDER BY action_id;
```

**Important**: If new teachers appear that aren't in the query above, dynamically add them. Detect teachers with:

```sql
SELECT DISTINCT teacher FROM labels;
```

## 3. Confidence distribution for the in-progress teacher

Identify which teacher(s) are still in progress (label count < total states) and bucket their confidence:

```sql
SELECT
  CASE
    WHEN confidence < 0.7 THEN '<0.70'
    WHEN confidence < 0.8 THEN '0.70-0.79'
    WHEN confidence < 0.9 THEN '0.80-0.89'
    ELSE '0.90+'
  END as conf_bucket,
  COUNT(*) as cnt,
  ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM labels WHERE teacher = '<IN_PROGRESS_TEACHER>'), 1) as pct
FROM labels
WHERE teacher = '<IN_PROGRESS_TEACHER>'
GROUP BY conf_bucket
ORDER BY conf_bucket;
```

## 4. Missing actions

Check which actions exist in `valid_action_ids` but are never chosen by any teacher:

```sql
SELECT DISTINCT action_id FROM labels
ORDER BY action_id;
```

Cross-reference against the 18-action space (0-17) and flag gaps.

## 5. Consensus readiness

```sql
SELECT COUNT(*) FROM consensus;
```

## Reporting format

Present results as:
1. **Progress summary** — X/Y states labelled per teacher, which are complete, which are in-progress
2. **Action distribution table** — all teachers side by side, percentages, highlight outliers
3. **Key observations** — mode collapse risks, missing actions, confidence anomalies, teacher agreement/disagreement patterns, any teacher that looks like an outlier (e.g., over-holding)
4. **Consensus readiness** — whether consensus has been built yet, and whether it should be

Keep it direct. Flag problems, don't bury them.
