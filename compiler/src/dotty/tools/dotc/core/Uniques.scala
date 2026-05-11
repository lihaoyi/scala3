package dotty.tools.dotc
package core

import Types.*, Contexts.*, util.Stats.*, Hashable.*, Names.*
import config.Config
import Symbols.Symbol
import Decorators.*
import util.{WeakHashSet, Stats}
import WeakHashSet.Entry
import scala.annotation.tailrec

class Uniques extends WeakHashSet[Type](Config.initialUniquesCapacity):
  override def hash(x: Type): Int = x.hash
  override def isEqual(x: Type, y: Type) = x.eql(y)

/** Defines operation `unique` for hash-consing types.
 *  Also defines specialized hash sets for hash consing uniques of a specific type.
 *  All sets offer a `enterIfNew` method which checks whether a type
 *  with the given parts exists already and creates a new one if not.
 */
object Uniques:

  private inline def recordCaching(tp: Type): Unit = recordCaching(tp.hash, tp.getClass)
  private inline def recordCaching(h: Int, clazz: Class[?]): Unit =
    if monitored then
      if h == NotCached then
        record("uncached-types")
        record(s"uncached: $clazz")
      else
        record("cached-types")
        record(s"cached: $clazz")

  def unique[T <: Type](tp: T)(using Context): T =
    recordCaching(tp)
    if tp.hash == NotCached then tp
    else ctx.uniques.put(tp).asInstanceOf[T]

  // Initial capacity doubled (was * 4) so the table avoids one resize cycle in
  // the early phases when the cache is coldest and lookups are densest. Mill
  // libs.javalib creates O(100k-1M) NamedTypes per Context; at the prior * 4
  // (128k slots, 0.5 load, resize at 64k entries) JFR shows
  // `NamedTypeUniques.linkedListLoop$1` at 1.98% self-time. Extra cost is
  // ~1MB array on heap per Context — negligible vs. type-graph memory.
  final class NamedTypeUniques extends WeakHashSet[NamedType](Config.initialUniquesCapacity * 8) with Hashable:
    override def hash(x: NamedType): Int = x.hash

    def enterIfNew(prefix: Type, designator: Designator, isTerm: Boolean)(using Context): NamedType =
      val h = doHash(null, designator, prefix)
      if monitored then recordCaching(h, classOf[NamedType])
      def newType =
        try
          if isTerm then new CachedTermRef(prefix, designator, h)
          else new CachedTypeRef(prefix, designator, h)
        catch case ex: InvalidPrefix => badPrefix(prefix, designator)
      if h == NotCached then newType
      else
        // Inlined from WeakHashSet#put. Uses the amortized `maybeRemoveStaleEntries`
        // (fires every ~256 ops) instead of `removeStaleEntries()`, to drop the
        // `ReferenceQueue.poll()` monitor enter from the hot type-construction path.
        Stats.record(statsItem("put"))
        maybeRemoveStaleEntries()
        val bucket = index(h)
        val oldHead = table(bucket)

        @tailrec
        def linkedListLoop(entry: Entry[NamedType] | Null): NamedType = entry match
          case null                    => addEntryAt(bucket, newType, h, oldHead)
          case _                       =>
            val e = entry.get
            if e != null && (e.prefix eq prefix) && (e.designator eq designator) && (e.isTerm == isTerm) then e
            else linkedListLoop(entry.tail)

        linkedListLoop(oldHead)
      end if
    end enterIfNew

    private def badPrefix(prefix: Type, desig: Designator)(using Context): Nothing =
      def name = desig match
        case desig: Name => desig
        case desig: Symbol => desig.name
      throw TypeError(em"invalid prefix $prefix when trying to form $prefix . $name")

  end NamedTypeUniques

  final class AppliedUniques extends WeakHashSet[AppliedType](Config.initialUniquesCapacity * 2) with Hashable:
    override def hash(x: AppliedType): Int = x.hash

    def enterIfNew(tycon: Type, args: List[Type]): AppliedType =
      val h = doHash(null, tycon, args)
      def newType = new CachedAppliedType(tycon, args, h)
      if monitored then recordCaching(h, classOf[CachedAppliedType])
      if h == NotCached then newType
      else
        // Inlined from WeakHashSet#put. Uses the amortized `maybeRemoveStaleEntries`
        // (fires every ~256 ops) instead of `removeStaleEntries()`, to drop the
        // `ReferenceQueue.poll()` monitor enter from the hot type-construction path.
        Stats.record(statsItem("put"))
        maybeRemoveStaleEntries()
        val bucket = index(h)
        val oldHead = table(bucket)

        @tailrec
        def linkedListLoop(entry: Entry[AppliedType] | Null): AppliedType = entry match
          case null                    => addEntryAt(bucket, newType, h, oldHead)
          case _                       =>
            val e = entry.get
            if e != null && (e.tycon eq tycon) && e.args.eqElements(args) then e
            else linkedListLoop(entry.tail)

        linkedListLoop(oldHead)
      end if
  end AppliedUniques
end Uniques
