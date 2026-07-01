import { Upload, Video, ListChecks } from "lucide-react";
import { Section, Reveal, Eyebrow } from "./primitives";

const steps = [
  {
    icon: Upload,
    step: "01",
    title: "Upload your process docs",
    body: "Connect the policies, playbooks, and procedures your team already relies on. Laura grounds every answer in them.",
  },
  {
    icon: Video,
    step: "02",
    title: "Invite Laura to the meeting",
    body: "Add Laura to Zoom, Google Meet, or Teams. It listens quietly and speaks only when called by name.",
  },
  {
    icon: ListChecks,
    step: "03",
    title: "Ask live, get a checklist",
    body: "Get grounded answers with citations during the call — then a summary, gap checklist, and follow-up email after.",
  },
];

export function Solution() {
  return (
    <Section id="solution">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <Eyebrow>How it works</Eyebrow>
        </Reveal>
        <Reveal delay={80}>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
            An AI process expert that joins the meeting.
          </h2>
        </Reveal>
      </div>

      <div className="relative mt-16 grid gap-8 md:grid-cols-3">
        {/* connecting line */}
        <div className="pointer-events-none absolute left-0 right-0 top-11 hidden h-px bg-gradient-to-r from-transparent via-border to-transparent md:block" />
        {steps.map((s, i) => (
          <Reveal key={s.step} delay={i * 120} className="relative">
            <div className="relative z-10 mx-auto flex h-[5.5rem] w-[5.5rem] items-center justify-center rounded-2xl border border-border bg-card shadow-card">
              <s.icon className="size-7 text-primary" />
            </div>
            <div className="mt-6 text-center">
              <span className="text-xs font-semibold uppercase tracking-[0.2em] text-primary">
                {s.step}
              </span>
              <h3 className="mt-2 text-xl font-semibold">{s.title}</h3>
              <p className="mx-auto mt-3 max-w-xs text-sm leading-relaxed text-muted-foreground">
                {s.body}
              </p>
            </div>
          </Reveal>
        ))}
      </div>
    </Section>
  );
}
