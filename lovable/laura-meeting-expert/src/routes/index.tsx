import { createFileRoute } from "@tanstack/react-router";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { Problem } from "@/components/landing/Problem";
import { Solution } from "@/components/landing/Solution";
import { Product } from "@/components/landing/Product";
import { UseCases } from "@/components/landing/UseCases";
import { Enterprise, Pricing } from "@/components/landing/Plans";
import { FinalCTA, Footer } from "@/components/landing/FinalCTA";

export const Route = createFileRoute("/")({
  component: Index,
});

function Index() {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <main>
        <Hero />
        <Problem />
        <Solution />
        <Product />
        <UseCases />
        <Pricing />
        <Enterprise />
        <FinalCTA />
      </main>
      <Footer />
    </div>
  );
}
