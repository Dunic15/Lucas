import { FolderLock, Brain, Mail } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Section, Reveal, Eyebrow } from "./primitives";

const problems = [
  {
    icon: FolderLock,
    title: "Policies are buried",
    body: "Procedures live in Notion, Confluence, SharePoint, Drive, and scattered PDFs — impossible to reach mid-conversation.",
  },
  {
    icon: Brain,
    title: "Teams forget steps",
    body: "In the moment, people rely on memory. Approvals, compliance checks, and handoffs quietly slip through.",
  },
  {
    icon: Mail,
    title: "Follow-ups are messy",
    body: "Action items are reconstructed from memory after the call, if they get written down at all.",
  },
];

export function Problem() {
  return (
    <Section id="problem" className="bg-gradient-soft">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <Eyebrow>The gap</Eyebrow>
        </Reveal>
        <Reveal delay={80}>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
            Process knowledge exists. Teams just don't use it live.
          </h2>
        </Reveal>
        <Reveal delay={140}>
          <p className="mt-5 text-lg leading-relaxed text-muted-foreground">
            Approvals, compliance steps, onboarding flows, customer handoffs, and internal
            procedures are often buried in Notion, Confluence, SharePoint, Google Drive, or PDFs.
            During meetings, people rely on memory — and critical steps get missed.
          </p>
        </Reveal>
      </div>

      <div className="mt-16 grid gap-6 md:grid-cols-3">
        {problems.map((p, i) => (
          <Reveal key={p.title} delay={i * 100}>
            <Card className="h-full border-border p-7 shadow-soft transition-all duration-300 hover:-translate-y-1 hover:shadow-card">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-accent-foreground">
                <p.icon className="size-5" />
              </div>
              <h3 className="mt-5 text-lg font-semibold">{p.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{p.body}</p>
            </Card>
          </Reveal>
        ))}
      </div>
    </Section>
  );
}
