import { FileText, ArrowRight, Video, ListChecks } from "lucide-react";
import { Section, Reveal, Eyebrow } from "./primitives";

const flow = [
  { icon: FileText, label: "Documents", sub: "Your process knowledge" },
  { icon: LauraNode, label: "Laura", sub: "Grounded reasoning", accent: true },
  { icon: Video, label: "Live meeting", sub: "Answers with citations" },
  { icon: ListChecks, label: "Checklist + email", sub: "Post-call follow-up" },
];

function LauraNode() {
  return <span className="text-base font-bold text-primary-foreground">L</span>;
}

export function Demo() {
  return (
    <Section id="demo" className="bg-gradient-soft">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <Eyebrow>The flow</Eyebrow>
        </Reveal>
        <Reveal delay={80}>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
            From process documents to live guidance.
          </h2>
        </Reveal>
      </div>

      <Reveal delay={120}>
        <div className="mt-16 flex flex-col items-stretch gap-4 md:flex-row md:items-center md:justify-between">
          {flow.map((node, i) => (
            <div
              key={node.label}
              className="flex flex-col items-center gap-4 md:flex-1 md:flex-row"
            >
              <div className="flex w-full flex-col items-center rounded-2xl border border-border bg-card p-6 text-center shadow-soft transition-all duration-300 hover:-translate-y-1 hover:shadow-card">
                <span
                  className={
                    node.accent
                      ? "flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-primary shadow-primary"
                      : "flex h-14 w-14 items-center justify-center rounded-2xl bg-accent text-accent-foreground"
                  }
                >
                  <node.icon className="size-6" />
                </span>
                <h3 className="mt-4 text-base font-semibold">{node.label}</h3>
                <p className="mt-1 text-xs text-muted-foreground">{node.sub}</p>
              </div>
              {i < flow.length - 1 && (
                <ArrowRight className="size-6 shrink-0 rotate-90 text-border md:rotate-0" />
              )}
            </div>
          ))}
        </div>
      </Reveal>
    </Section>
  );
}
