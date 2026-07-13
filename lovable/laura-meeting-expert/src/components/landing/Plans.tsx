import { ArrowRight, Check, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Eyebrow, Reveal, SALES_LINK, Section, START_LINK } from "./primitives";

const freeFeatures = [
  "15 lifetime avatar-minutes",
  "Laura and Cedric",
  "Google Meet, Zoom and Microsoft Teams",
  "Meeting recap, decisions and action items",
];

const soloFeatures = [
  "300 avatar-minutes each month",
  "One balance shared by Laura and Cedric",
  "Connected tools through Cedric",
  "Meeting history and billing portal",
];

function FeatureList({ items }: { items: string[] }) {
  return (
    <ul className="mt-7 space-y-3 text-sm text-muted-foreground">
      {items.map((item) => (
        <li key={item} className="flex items-start gap-2.5">
          <Check className="mt-0.5 !size-4 shrink-0 text-primary" aria-hidden="true" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

export function Pricing() {
  return (
    <Section id="pricing" className="bg-gradient-soft">
      <Reveal className="mx-auto max-w-3xl text-center">
        <Eyebrow>For individuals</Eyebrow>
        <h2 className="mt-6 text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
          Try the meeting, then choose the plan.
        </h2>
        <p className="mx-auto mt-5 max-w-2xl text-lg text-muted-foreground">
          Your minutes are shared across Laura and Cedric. No surprise overage: when your
          allowance ends, the avatar leaves safely and your meeting output is preserved.
        </p>
      </Reveal>

      <div className="mx-auto mt-14 grid max-w-4xl gap-5 md:grid-cols-2">
        <Reveal as="article" className="rounded-3xl border border-border bg-card p-7 shadow-card">
          <p className="text-sm font-semibold text-primary">Free</p>
          <div className="mt-3 text-4xl font-semibold tracking-tight">€0</div>
          <p className="mt-2 text-sm text-muted-foreground">No card required</p>
          <FeatureList items={freeFeatures} />
          <Button asChild variant="soft" size="lg" className="mt-8 w-full">
            <a href={START_LINK}>Start with 15 minutes</a>
          </Button>
        </Reveal>

        <Reveal
          as="article"
          delay={80}
          className="relative overflow-hidden rounded-3xl border border-primary/35 bg-card p-7 shadow-elevated"
        >
          <div className="pointer-events-none absolute -right-24 -top-24 h-56 w-56 rounded-full bg-gradient-primary opacity-15 blur-3xl" />
          <p className="relative text-sm font-semibold text-primary">Solo</p>
          <div className="relative mt-3 text-4xl font-semibold tracking-tight">
            €49 <span className="text-base font-medium text-muted-foreground">/ month</span>
          </div>
          <p className="relative mt-2 text-sm text-muted-foreground">For regular meeting use</p>
          <FeatureList items={soloFeatures} />
          <Button asChild variant="hero" size="lg" className="relative mt-8 w-full">
            <a href={START_LINK}>
              Start free first <ArrowRight />
            </a>
          </Button>
        </Reveal>
      </div>
    </Section>
  );
}

export function Enterprise() {
  return (
    <Section id="enterprise">
      <Reveal>
        <div className="relative overflow-hidden rounded-[2rem] border border-border bg-card px-7 py-12 shadow-elevated sm:px-12 md:py-16">
          <div className="pointer-events-none absolute -right-20 -top-36 h-80 w-80 rounded-full bg-gradient-primary opacity-10 blur-3xl" />
          <div className="relative grid items-center gap-10 lg:grid-cols-[1fr_auto]">
            <div>
              <div className="flex flex-wrap items-center gap-3">
                <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent text-accent-foreground">
                  <ShieldCheck aria-hidden="true" />
                </span>
                <Eyebrow>Enterprise early access</Eyebrow>
              </div>
              <h2 className="mt-6 max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">
                Help shape Laura for security-conscious teams.
              </h2>
              <p className="mt-5 max-w-3xl text-lg leading-relaxed text-muted-foreground">
                We are onboarding design partners for shared workspaces, SSO, roles,
                auditability, retention controls, procurement support and guided rollout.
                These controls are early access, not a separate dashboard mode today.
              </p>
            </div>
            <Button asChild variant="soft" size="xl" className="whitespace-nowrap">
              <a href={SALES_LINK}>Talk to us</a>
            </Button>
          </div>
        </div>
      </Reveal>
    </Section>
  );
}
