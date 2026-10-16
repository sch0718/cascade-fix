export const meta = {
  name: 'cascade-fix-jobs',
  description: '지적별 작업자·판가름 호출',
  phases: [{ title: 'Jobs' }],
}

// args: {jobs: [{id, label, model, effort, prompt, schema}]}
// 반환: {results: [{id, output}]} — output은 schema를 만족하는 객체, 실패·건너뜀이면 null

const jobs = args.jobs ?? []
log(`jobs: ${jobs.length}건`)

const results = await pipeline(jobs, j =>
  agent(j.prompt, {
    label: j.label,
    model: j.model,
    effort: j.effort,
    schema: j.schema,
    phase: 'Jobs',
  }).then(o => ({ id: j.id, output: o }))
)

return { results }
