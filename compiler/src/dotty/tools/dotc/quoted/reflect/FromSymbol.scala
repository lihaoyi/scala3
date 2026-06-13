package dotty.tools.dotc.quoted
package reflect

import dotty.tools.dotc.ast.tpd
import dotty.tools.dotc.ast.untpd
import dotty.tools.dotc.core.Contexts.*
import dotty.tools.dotc.core.Flags.*
import dotty.tools.dotc.core.StdNames.*
import dotty.tools.dotc.core.Symbols.*
import dotty.tools.dotc.core.Types.*

object FromSymbol {

  def definitionFromSym(sym: Symbol)(using Context): tpd.Tree = {
    assert(sym.exists, "Cannot get tree of no symbol")
    assert(!sym.is(Package), "Cannot get tree of package symbol")
    sym.defTree match {
      case tpd.EmptyTree =>
        val run = ctx.run
        if run == null then synthesizedDefinitionFromSym(sym)
        else run.fromSymbolDefinitionCache.getOrElseUpdate(sym, synthesizedDefinitionFromSym(sym))
      case _ =>
        uncachedDefinitionFromSym(sym)
    }
  }

  private def uncachedDefinitionFromSym(sym: Symbol)(using Context): tpd.Tree =
    if (sym.isClass) classDef(sym.asClass)
    else if (sym.isType && sym.is(Case)) typeBindFromSym(sym.asType)
    else if (sym.isType) typeDefFromSym(sym.asType)
    else if (sym.is(Method)) defDefFromSym(sym.asTerm)
    else if (sym.is(Case, butNot = ModuleVal | EnumVal)) bindFromSym(sym.asTerm)
    else valDefFromSym(sym.asTerm)

  private def synthesizedDefinitionFromSym(sym: Symbol)(using Context): tpd.Tree =
    if (sym.isClass) synthesizedClassDef(sym.asClass)
    else if (sym.isType && sym.is(Case)) synthesizedTypeBindFromSym(sym.asType)
    else if (sym.isType) tpd.TypeDef(sym.asType)
    else if (sym.is(Method)) tpd.DefDef(sym.asTerm)
    else if (sym.is(Case, butNot = ModuleVal | EnumVal)) synthesizedBindFromSym(sym.asTerm)
    else tpd.ValDef(sym.asTerm)

  def classDef(cls: ClassSymbol)(using Context): tpd.TypeDef = cls.defTree match {
    case tree: tpd.TypeDef => tree
    case tpd.EmptyTree => synthesizedClassDef(cls)
  }

  private def synthesizedClassDef(cls: ClassSymbol)(using Context): tpd.TypeDef = {
    val constrSym = cls.unforcedDecls.find(_.isPrimaryConstructor).orElse(
      // Dummy constructor for classes such as `<refinement>`
      newSymbol(cls, nme.CONSTRUCTOR, EmptyFlags, NoType)
    )
    val constr = tpd.DefDef(constrSym.asTerm)
    val parents = cls.info.parents.map(tpd.TypeTree(_))
    val body = cls.unforcedDecls.filter(!_.isPrimaryConstructor).map(s => definitionFromSym(s))
    tpd.ClassDefWithParents(cls, constr, parents, body)
  }

  def typeDefFromSym(sym: TypeSymbol)(using Context): tpd.TypeDef = sym.defTree match {
    case tree: tpd.TypeDef => tree
    case tpd.EmptyTree => tpd.TypeDef(sym)
  }

  def defDefFromSym(sym: TermSymbol)(using Context): tpd.DefDef = sym.defTree match {
    case tree: tpd.DefDef => tree
    case tpd.EmptyTree => tpd.DefDef(sym)
  }

  def valDefFromSym(sym: TermSymbol)(using Context): tpd.ValOrDefDef = sym.defTree match {
    case tree: tpd.ValDef => tree
    case tree: tpd.DefDef =>
      // `Getters` phase replaces val class members with defs,
      // so we may see a defdef here if we are running this on a symbol compiled
      // in the same compilation (but before suspension, so that
      // the symbol could have reached `Getters`).
      tree
    case tpd.EmptyTree => tpd.ValDef(sym)
  }

  def bindFromSym(sym: TermSymbol)(using Context): tpd.Bind = sym.defTree match {
    case tree: tpd.Bind => tree
    case tpd.EmptyTree => synthesizedBindFromSym(sym)
  }

  def typeBindFromSym(sym: TypeSymbol)(using Context): tpd.Bind = sym.defTree match {
    case tree: tpd.Bind => tree
    case tpd.EmptyTree => synthesizedTypeBindFromSym(sym)
  }

  private def synthesizedBindFromSym(sym: TermSymbol)(using Context): tpd.Bind =
    tpd.Bind(sym, untpd.Ident(nme.WILDCARD).withType(sym.typeRef))

  private def synthesizedTypeBindFromSym(sym: TypeSymbol)(using Context): tpd.Bind =
    tpd.Bind(sym, untpd.Ident(nme.WILDCARD).withType(sym.typeRef))
}
