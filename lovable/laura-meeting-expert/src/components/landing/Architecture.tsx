import { FileCheck2, AlertTriangle, Ban, Building2, Link2 } from "lucide-react";
import { Section, Reveal, Eyebrow } from "./primitives";

const trustPoints = [
  { icon: FileCheck2, label: "Grounded in approved documents" },
  { icon: Link2, label: "Cites its sources" },
  { icon: AlertTriangle, label: "Conservative when uncertain" },
  { icon: Ban, label: "Does not guess" },
  { icon: Building2, label: "Designed for enterprise processes" },
];

export function Architecture() {
  return (
    <Section id="architecture">
      <div className="grid gap-14 lg:grid-cols-[0.95fr_1.05fr] lg:items-center">
        <div>
          <Reveal>
            <Eyebrow>Trust & architecture</Eyebrow>
          </Reveal>
          <Reveal delay={80}>
            <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
              Built for serious company workflows.
            </h2>
          </Reveal>
          <Reveal delay={140}>
            <p className="mt-5 text-lg leading-relaxed text-muted-foreground">
              Laura separates the brain from the avatar. The reasoning engine stays in the backend,
              grounded in your company knowledge. The avatar is only the interface — swappable,
              optional, and controlled.
            </p>
          </Reveal>

          <div className="mt-8 space-y-3">
            {trustPoints.map((t, i) => (
              <Reveal key={t.label} delay={i * 70}>
                <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-xs">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-foreground">
                    <t.icon className="size-4" />
                  </span>
                  <span className="text-sm font-medium">{t.label}</span>
                </div>
              </Reveal>
            ))}
          </div>
        </div>

        <Reveal delay={160}>
          <div className="relative rounded-3xl border border-border bg-gradient-soft p-8 shadow-card">
            <BrainDiagram />
          </div>
        </Reveal>
      </div>
    </Section>
  );
}

function BrainDiagram() {
  return (
    <div className="space-y-5">
      <div className="rounded-2xl border border-primary/20 bg-card p-5 shadow-soft">
        <span className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">
          Reasoning engine · backend
        </span>
        <p className="mt-2 text-sm text-muted-foreground">
          Grounded in your approved company knowledge. Cites sources, refuses to guess, escalates
          when uncertain.
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          {["Process docs", "Citations", "Guardrails"].map((chip) => (
            <span
              key={chip}
              className="rounded-md bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground"
            >
              {chip}
            </span>
          ))}
        </div>
      </div>

      <div className="flex justify-center">
        <span className="h-6 w-px bg-border" />
      </div>

      <div className="rounded-2xl border border-border bg-card p-5 shadow-soft">
        <span className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
          Avatar · interface
        </span>
        <p className="mt-2 text-sm text-muted-foreground">
          Only the interface — swappable, optional, and fully controlled. Never the source of truth.
        </p>
      </div>
    </div>
  );
}
