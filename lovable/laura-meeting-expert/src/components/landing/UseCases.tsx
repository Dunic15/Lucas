import { UserPlus, FileSignature, Presentation, Settings2, ScrollText, Users } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Section, Reveal, Eyebrow } from "./primitives";

const useCases = [
  {
    icon: UserPlus,
    title: "Customer onboarding",
    body: "Guide teams through every onboarding step, live.",
  },
  {
    icon: FileSignature,
    title: "Procurement approvals",
    body: "Never skip a required approval or sign-off.",
  },
  {
    icon: Presentation,
    title: "Sales engineering calls",
    body: "Answer technical questions from trusted docs.",
  },
  {
    icon: Settings2,
    title: "Internal operations",
    body: "Keep recurring processes consistent across teams.",
  },
  {
    icon: ScrollText,
    title: "Compliance-heavy workflows",
    body: "Enforce steps where mistakes are costly.",
  },
  {
    icon: Users,
    title: "HR & manager processes",
    body: "Support managers with the right procedure, on demand.",
  },
];

export function UseCases() {
  return (
    <Section id="use-cases">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <Eyebrow>Use cases</Eyebrow>
        </Reveal>
        <Reveal delay={80}>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
            Where Laura helps first
          </h2>
        </Reveal>
      </div>

      <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {useCases.map((u, i) => (
          <Reveal key={u.title} delay={(i % 3) * 90}>
            <Card className="group h-full border-border p-7 shadow-soft transition-all duration-300 hover:-translate-y-1 hover:shadow-card">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-accent-foreground transition-colors group-hover:bg-gradient-primary group-hover:text-primary-foreground">
                <u.icon className="size-5" />
              </div>
              <h3 className="mt-5 text-lg font-semibold">{u.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{u.body}</p>
            </Card>
          </Reveal>
        ))}
      </div>
    </Section>
  );
}
