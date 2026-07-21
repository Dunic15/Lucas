import { ArrowRight, FileText, Mic, Video, Users, MessageSquare, PhoneOff } from "lucide-react";
import meetingStartImg from "@/assets/laura-meeting-start.png";
import { Button } from "@/components/ui/button";
import { DEMO_LINK, Eyebrow, Reveal } from "./primitives";

export function Hero() {
  return (
    <section
      id="top"
      className="relative overflow-hidden bg-gradient-hero px-6 pb-24 pt-36 sm:px-8 md:pb-32 md:pt-44"
    >
      <div className="mx-auto grid w-full max-w-6xl items-center gap-16 lg:grid-cols-[1.05fr_0.95fr]">
        <div className="max-w-2xl">
          <Reveal>
            <Eyebrow>Live AI Process Expert</Eyebrow>
          </Reveal>
          <Reveal delay={80}>
            <h1 className="mt-6 text-4xl font-semibold leading-[1.05] tracking-tight sm:text-5xl md:text-6xl">
              Your company's process expert,{" "}
              <span className="text-gradient">live in every meeting.</span>
            </h1>
          </Reveal>
          <Reveal delay={160}>
            <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted-foreground">
              Laura is a live AI avatar that joins your Zoom, Meet, or Teams call, listens in, and
              answers from trusted company knowledge; so teams never miss a critical step.
            </p>
          </Reveal>
          <Reveal delay={240}>
            <div className="mt-9 flex flex-col gap-3 sm:flex-row">
              <Button asChild variant="hero" size="xl">
                <a href={DEMO_LINK}>
                  Book a demo
                  <ArrowRight />
                </a>
              </Button>
              <Button asChild variant="soft" size="xl">
                <a href="#solution">See how it works</a>
              </Button>
            </div>
          </Reveal>
          <Reveal delay={320}>
            <div className="mt-10 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-muted-foreground">
              <span className="font-medium text-foreground/70">Works with</span>
              <span>Zoom</span>
              <span className="h-1 w-1 rounded-full bg-border" />
              <span>Google Meet</span>
              <span className="h-1 w-1 rounded-full bg-border" />
              <span>Microsoft Teams</span>
            </div>
          </Reveal>
        </div>

        <Reveal delay={200}>
          <HeroMockup />
        </Reveal>
      </div>
    </section>
  );
}

function HeroMockup() {
  return (
    <div className="relative animate-float">
      <div className="absolute -inset-6 -z-10 rounded-[2rem] bg-gradient-primary opacity-10 blur-2xl" />

      {/* Zoom-style meeting window */}
      <div className="overflow-hidden rounded-3xl border border-border bg-foreground shadow-elevated">
        {/* top bar */}
        <div className="flex items-center justify-between px-4 py-2.5">
          <div className="flex items-center gap-3 text-xs font-medium">
            <span className="inline-flex items-center gap-1.5 text-background/80">
              <span className="h-2 w-2 animate-pulse rounded-full bg-destructive" />
              <span className="uppercase tracking-wide">Rec</span>
            </span>
            <span className="text-background/50">Weekly ops sync · 24:18</span>
          </div>
          <span className="inline-flex items-center gap-1 text-xs text-background/60">
            <Users className="!size-3.5" /> 5
          </span>
        </div>

        {/* main speaker tile */}
        <div className="relative mx-3 aspect-video overflow-hidden rounded-xl ring-2 ring-primary/70">
          <img
            src={meetingStartImg}
            alt="Realistic hybrid meeting with Laura joining as an AI expert"
            width={1792}
            height={1024}
            className="h-full w-full object-cover"
          />
          {/* live caption (question to Laura) */}
          <div className="absolute inset-x-3 bottom-12 mx-auto max-w-[92%] rounded-lg bg-foreground/80 px-3 py-2 text-center text-xs leading-relaxed text-background backdrop-blur">
            "Laura, what's the approval step before we sign a new vendor?"
          </div>
          {/* name tag */}
          <div className="absolute bottom-2 left-2 inline-flex items-center gap-1.5 rounded-md bg-foreground/70 px-2 py-1 text-xs font-medium text-background backdrop-blur">
            <Mic className="!size-3 text-primary" /> Laura
          </div>
        </div>

        {/* participant strip */}
        <div className="mt-3 flex gap-2 px-3">
          {/* Laura AI tile */}
          <div className="relative flex aspect-video flex-1 items-center justify-center overflow-hidden rounded-lg bg-gradient-primary">
            <span className="absolute -inset-2 animate-ping rounded-full bg-primary-foreground/10" />
            <span className="relative flex h-9 w-9 items-center justify-center rounded-full bg-primary-foreground/20 text-sm font-bold text-primary-foreground">
              L
            </span>
            <span className="absolute bottom-1 left-1 inline-flex items-center gap-1 rounded bg-background/85 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-foreground">
              <Mic className="!size-2.5 text-primary" /> Laura
            </span>
          </div>
          {/* muted participants */}
          {[
            { initials: "SK", tone: "bg-secondary" },
            { initials: "MA", tone: "bg-muted" },
          ].map((p) => (
            <div
              key={p.initials}
              className={`relative flex aspect-video flex-1 items-center justify-center overflow-hidden rounded-lg ${p.tone}`}
            >
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-foreground/10 text-sm font-semibold text-foreground/70">
                {p.initials}
              </span>
            </div>
          ))}
        </div>

        {/* meeting toolbar */}
        <div className="mt-3 flex items-center justify-center gap-4 border-t border-background/10 px-4 py-3 text-background/70">
          <Mic className="!size-4" />
          <Video className="!size-4" />
          <Users className="!size-4" />
          <MessageSquare className="!size-4" />
          <span className="ml-1 inline-flex items-center gap-1.5 rounded-md bg-destructive px-2.5 py-1 text-xs font-semibold text-destructive-foreground">
            <PhoneOff className="!size-3.5" /> Leave
          </span>
        </div>
      </div>

      {/* Laura grounded answer + source */}
      <div className="mt-4 rounded-2xl border border-border bg-card p-4 shadow-elevated">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span className="flex h-6 w-6 items-center justify-center rounded-md bg-gradient-primary text-[11px] font-bold text-primary-foreground">
            L
          </span>
          Laura
          <span className="inline-flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent-foreground">
            <Mic className="!size-3" /> Speaking
          </span>
        </div>
        <p className="mt-2.5 text-sm leading-relaxed text-foreground/90">
          "Finance approval is required before the contract is signed; that's step 3 of the
          procurement policy."
        </p>
        <div className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border bg-secondary/60 px-2.5 py-1 text-xs text-muted-foreground">
          <FileText className="!size-3.5 text-primary" />
          Procurement_Policy.pdf · p.4
        </div>
      </div>
    </div>
  );
}
