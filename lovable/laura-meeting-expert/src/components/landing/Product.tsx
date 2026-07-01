import { Radio, Mic, Quote, SearchCheck, ClipboardCheck, MailPlus, Video } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Section, Reveal, Eyebrow } from "./primitives";

const features = [
  {
    icon: Radio,
    title: "Live meeting presence",
    body: "Laura joins the call as a participant and follows the conversation in real time.",
  },
  {
    icon: Mic,
    title: "Wake-word activation",
    body: "It stays silent until called by name — no interruptions, no noise.",
  },
  {
    icon: Quote,
    title: "Grounded answers with citations",
    body: "Every response is backed by your approved documents, with the source shown.",
  },
  {
    icon: SearchCheck,
    title: "Process gap detection",
    body: "Laura flags missed steps and required approvals before they become problems.",
  },
  {
    icon: ClipboardCheck,
    title: "Summary & action checklist",
    body: "A clean recap and a step-by-step checklist generated automatically after the call.",
  },
  {
    icon: MailPlus,
    title: "Follow-up email draft",
    body: "A ready-to-send follow-up email so nothing falls through the cracks.",
  },
];

export function Product() {
  return (
    <Section id="product" className="bg-gradient-soft">
      <div className="mx-auto max-w-3xl text-center">
        <Reveal>
          <Eyebrow>Capabilities</Eyebrow>
        </Reveal>
        <Reveal delay={80}>
          <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
            What Laura does
          </h2>
        </Reveal>
      </div>

      <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {features.map((f, i) => (
          <Reveal key={f.title} delay={(i % 3) * 90}>
            <Card className="h-full border-border p-7 shadow-soft transition-all duration-300 hover:-translate-y-1 hover:shadow-card">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-primary text-primary-foreground shadow-primary">
                <f.icon className="size-5" />
              </div>
              <h3 className="mt-5 text-lg font-semibold">{f.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{f.body}</p>
            </Card>
          </Reveal>
        ))}
      </div>

      <Reveal delay={120}>
        <div className="mt-8 flex flex-col items-center justify-center gap-3 rounded-2xl border border-border bg-card p-6 text-center shadow-soft sm:flex-row">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent text-accent-foreground">
            <Video className="size-5" />
          </span>
          <p className="text-base font-medium">
            Works seamlessly with <span className="font-semibold">Zoom</span>,{" "}
            <span className="font-semibold">Google Meet</span>, and{" "}
            <span className="font-semibold">Microsoft Teams</span>.
          </p>
        </div>
      </Reveal>
    </Section>
  );
}
