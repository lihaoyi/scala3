package dotty.tools.dotc.quoted

import dotty.tools.dotc.ast.tpd
import dotty.tools.dotc.core.Contexts.*
import dotty.tools.dotc.typer.Typer
import dotty.tools.dotc.util.{Property, SourcePosition}

object MacroExpansion {

  private val MacroExpansionPosition = new Property.Key[SourcePosition]

  def position(using Context): Option[SourcePosition] =
    ctx.property(MacroExpansionPosition)

  def context(inlinedFrom: tpd.Tree)(using Context): Context =
    // Reuse the run-scoped quotes cache so that repeated macro expansions do
    // not re-unpickle the same pickled quote payloads. The cache lives for the
    // duration of the current run and is dropped with it.
    val run = ctx.run
    val cache = if run == null then QuotesCache.mkCache() else run.quotesCache
    QuotesCache.init(ctx.fresh, cache).setProperty(MacroExpansionPosition, inlinedFrom.sourcePos).setTypeAssigner(new Typer(ctx.nestingLevel + 1)).withSource(inlinedFrom.source)
}

