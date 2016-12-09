from snorkel.candidates import OmniNgrams
from snorkel.models import TemporaryImplicitSpan, CandidateSet, AnnotationKey, AnnotationKeySet, Label
from snorkel.matchers import RegexMatchSpan, Union
from snorkel.utils import ProgressBar
from snorkel.loaders import create_or_fetch
from snorkel.lf_helpers import *
import csv
import codecs
import re
import os
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import chain

# eeca_matcher = RegexMatchSpan(rgx='([b]{1}[abcdefklnpqruyz]{1}[\swxyz]?[0-9]{3,5}[\s]?[A-Z\/]{0,5}[0-9]?[A-Z]?([-][A-Z0-9]{1,7})?([-][A-Z0-9]{1,2})?)')
# jedec_matcher = RegexMatchSpan(rgx='([123]N\d{3,4}[A-Z]{0,5}[0-9]?[A-Z]?)')
# jis_matcher = RegexMatchSpan(rgx='(2S[abcdefghjkmqrstvz]{1}[\d]{2,4})')
# others_matcher = RegexMatchSpan(rgx='((NSVBC|SMBT|MJ|MJE|MPS|MRF|RCA|TIP|ZTX|ZT|TIS|TIPL|DTC|MMBT|PZT){1}[\d]{2,4}[A-Z]{0,3}([-][A-Z0-9]{0,6})?([-][A-Z0-9]{0,1})?)')
# part_matcher = Union(eeca_matcher, jedec_matcher, jis_matcher, others_matcher)

def get_part_throttler_wrapper():
    """get a part throttler wrapper to throttler unary candidates with the usual binary throttler"""
    def part_throttler_wrapper(part):
        return part_throttler((part[0], None))
    return part_throttler_wrapper

def get_part_throttler():
    return part_throttler

def part_throttler((part, attr)):
    """throttle parts that are in tables of device/replacement parts"""
    aligned_ngrams = set(get_aligned_ngrams(part))
    # if (overlap(['replacement', 'marking', 'mark'], aligned_ngrams) or
    if (overlap(['replacement'], aligned_ngrams) or
        (len(aligned_ngrams) > 25 and 'device' in aligned_ngrams) or
        # CentralSemiconductorCorp_2N4013.pdf:
        get_prev_sibling_tags(part).count('p') > 25 or
        overlap(['complementary', 'complement', 'empfohlene'], 
                chain.from_iterable([
                    get_left_ngrams(part, window=10),
                    get_aligned_ngrams(part)]))):
        return False
    else:
        return True

def get_part_matcher():
    eeca_matcher = RegexMatchSpan(rgx='([ABC][A-Z][WXYZ]?[0-9]{3,5}(?:[A-Z]){0,5}[0-9]?[A-Z]?(?:-[A-Z0-9]{1,7})?(?:[-][A-Z0-9]{1,2})?(?:\/DG)?)')
    jedec_matcher = RegexMatchSpan(rgx='(2N\d{3,4}[A-Z]{0,5}[0-9]?[A-Z]?)')
    jis_matcher = RegexMatchSpan(rgx='(2S[ABCDEFGHJKMQRSTVZ]{1}[\d]{2,4})')
    others_matcher = RegexMatchSpan(rgx='((?:NSVBC|SMBT|MJ|MJE|MPS|MRF|RCA|TIP|ZTX|ZT|ZXT|TIS|TIPL|DTC|MMBT|SMMBT|PZT|FZT){1}[\d]{2,4}[A-Z]{0,3}(?:-[A-Z0-9]{0,6})?(?:[-][A-Z0-9]{0,1})?)')
    return Union(eeca_matcher, jedec_matcher, jis_matcher, others_matcher)

class OmniNgramsTemp(OmniNgrams):
    def __init__(self, n_max=5, split_tokens=None):
        OmniNgrams.__init__(self, n_max=n_max, split_tokens=None)

    def apply(self, context):
        for ts in OmniNgrams.apply(self, context):
            m = re.match(u'^([\+\-\u2010\u2011\u2012\u2013\u2014\u2212\uf02d])?(\s*)(\d+)$', ts.get_span(), re.U)
            if m:
                if m.group(1) is None:
                    temp = ''
                elif m.group(1) == '+':
                    if m.group(2) != '':
                        continue # If bigram '+ 150' is seen, accept the unigram '150', not both
                    temp = ''
                else: # m.group(1) is a type of negative sign
                    # A bigram '- 150' is different from unigram '150', so we keep the implicit '-150'
                    temp = '-'
                temp += m.group(3)
                yield TemporaryImplicitSpan(
                    parent         = ts.parent,
                    char_start     = ts.char_start,
                    char_end       = ts.char_end,
                    expander_key   = u'temp_expander',
                    position       = 0,
                    text           = temp,
                    words          = [temp],
                    lemmas         = [temp],
                    pos_tags       = [ts.get_attrib_tokens('pos_tags')[-1]],
                    ner_tags       = [ts.get_attrib_tokens('ner_tags')[-1]],
                    dep_parents    = [ts.get_attrib_tokens('dep_parents')[-1]],
                    dep_labels     = [ts.get_attrib_tokens('dep_labels')[-1]],
                    page           = [ts.get_attrib_tokens('page')[-1]] if ts.parent.is_visual() else [None],
                    top            = [ts.get_attrib_tokens('top')[-1]] if ts.parent.is_visual() else [None],
                    left           = [ts.get_attrib_tokens('left')[-1]] if ts.parent.is_visual() else [None],
                    bottom         = [ts.get_attrib_tokens('bottom')[-1]] if ts.parent.is_visual() else [None],
                    right          = [ts.get_attrib_tokens('right')[-1]] if ts.parent.is_visual() else [None],
                    meta           = None)
            else:
                yield ts



class OmniNgramsPart(OmniNgrams):
    def __init__(self, parts_by_doc=None, n_max=5, split_tokens=None):
        """:param parts_by_doc: a dictionary d where d[document_name.upper()] = [partA, partB, ...]"""
        OmniNgrams.__init__(self, n_max=n_max, split_tokens=None)
        self.parts_by_doc = parts_by_doc

    def apply(self, context):
        for ts in OmniNgrams.apply(self, context):
            enumerated_parts = [part.upper() for part in expand_part_range(ts.get_span())]
            parts = set(enumerated_parts)
            if self.parts_by_doc:
                possible_parts =  self.parts_by_doc[ts.parent.document.name.upper()]
                for base_part in enumerated_parts:
                    for part in possible_parts:
                        if part.startswith(base_part) and len(base_part) >= 4:
                            parts.add(part)
            for i, part in enumerate(parts):
                if ' ' in part:
                    continue # it won't pass the part_matcher
                # TODO: Is this try/except necessary?
                try:
                    part.decode('ascii')
                except:
                    continue
                if part == ts.get_span():
                    yield ts
                else:
                    yield TemporaryImplicitSpan(
                        parent         = ts.parent,
                        char_start     = ts.char_start,
                        char_end       = ts.char_end,
                        expander_key   = u'part_expander',
                        position       = i,
                        text           = part,
                        words          = [part],
                        lemmas         = [part],
                        pos_tags       = [ts.get_attrib_tokens('pos_tags')[0]],
                        ner_tags       = [ts.get_attrib_tokens('ner_tags')[0]],
                        dep_parents    = [ts.get_attrib_tokens('dep_parents')[0]],
                        dep_labels     = [ts.get_attrib_tokens('dep_labels')[0]],
                        page           = [min(ts.get_attrib_tokens('page'))] if ts.parent.is_visual() else [None],
                        top            = [min(ts.get_attrib_tokens('top'))] if ts.parent.is_visual() else [None],
                        left           = [max(ts.get_attrib_tokens('left'))] if ts.parent.is_visual() else [None],
                        bottom         = [min(ts.get_attrib_tokens('bottom'))] if ts.parent.is_visual() else [None],
                        right          = [max(ts.get_attrib_tokens('right'))] if ts.parent.is_visual() else [None],
                        meta           = None
                    )

def load_hardware_doc_part_pairs(filename):
    with open(filename, 'r') as csvfile:
        gold_reader = csv.reader(csvfile)
        gold = set()
        for row in gold_reader:
            (doc, part, attr, val) = row
            gold.add((doc.upper(), part.upper()))
        return gold


def get_gold_parts(filename, docs=None):
    return set(map(lambda x: x[0], get_gold_dict(filename, doc_on=False, part_on=True, val_on=False, docs=docs)))


def get_gold_dict(filename, doc_on=True, part_on=True, val_on=True, attrib=None, docs=None, integerize=False):
    with codecs.open(filename, encoding="utf-8") as csvfile:
        gold_reader = csv.reader(csvfile)
        gold_dict = set()
        for row in gold_reader:
            (doc, part, attr, val) = row
            if docs is None or doc.upper() in docs:
                if attrib and attr != attrib:
                    continue
                if not val:
                    continue
                else:
                    key = []
                    if doc_on:  key.append(doc.upper())
                    if part_on: key.append(part.upper())
                    if val_on:
                        if integerize:
                            key.append(int(float(val)))
                        else:
                            key.append(val.upper())
                    gold_dict.add(tuple(key))
    return gold_dict


def count_hardware_labels(candidates, filename, attrib, attrib_class):
    gold_dict = get_gold_dict(filename, attrib)
    gold_cand = defaultdict(int)
    pb = ProgressBar(len(candidates))
    for i, c in enumerate(candidates):
        pb.bar(i)
        key = ((c[0].parent.document.name).upper(), (c[0].get_span()).upper(), (''.join(c[1].get_span().split())).upper())
        if key in gold_dict:
            gold_cand[key] += 1
    pb.close()
    return gold_cand


def load_hardware_labels(session, label_set_name, annotation_key_name, candidates, filename, attrib):
    gold_dict = get_gold_dict(filename, attrib=attrib)
    candidate_set   = create_or_fetch(session, CandidateSet, label_set_name)
    annotation_key  = create_or_fetch(session, AnnotationKey, annotation_key_name)
    key_set         = create_or_fetch(session, AnnotationKeySet, annotation_key_name)
    if annotation_key not in key_set.keys:
        key_set.append(annotation_key)
    session.commit()

    cand_total = len(candidates)
    print 'Loading', cand_total, 'candidate labels'
    pb = ProgressBar(cand_total)
    for i, c in enumerate(candidates):
        pb.bar(i)
        doc = (c[0].parent.document.name).upper()
        part = (c[0].get_span()).upper()
        val = (''.join(c[1].get_span().split())).upper()
        if (doc, part, val) in gold_dict:
            candidate_set.append(c)
            session.add(Label(key=annotation_key, candidate=c, value=1))
    session.commit()
    pb.close()
    return (candidate_set, annotation_key)


def most_common_document(candidates):
    """Returns the document that produced the most of the passed-in candidates"""
    # Turn CandidateSet into set of tuples
    pb = ProgressBar(len(candidates))
    candidate_count = {}

    for i, c in enumerate(candidates):
        pb.bar(i)
        part = c.get_arguments()[0].get_span()
        doc = c.get_arguments()[0].parent.document.name
        candidate_count[doc] = candidate_count.get(doc, 0) + 1 # count number of occurences of keys
    pb.close()
    max_doc = max(candidate_count, key=candidate_count.get)
    return max_doc


def entity_confusion_matrix(pred, gold):
    if not isinstance(pred, set):
        pred = set(pred)
    if not isinstance(gold, set):
        gold = set(gold)
    TP = pred.intersection(gold)
    FP = pred.difference(gold)
    FN = gold.difference(pred)
    return (TP, FP, FN)


def entity_level_total_recall(candidates, gold_file, attribute, corpus=None, 
                              relation=True, parts_by_doc=None, integerize=False):
    """Checks entity-level recall of candidates compared to gold.

    Turns a CandidateSet into a normal set of entity-level tuples
    (doc, part, [attribute_value])
    then compares this to the entity-level tuples found in the gold.

    Example Usage:
        from hardware_utils import entity_level_total_recall
        candidates = # CandidateSet of all candidates you want to consider
        gold_file = os.environ['SNORKELHOME'] + '/tutorials/tables/data/hardware/hardware_gold.csv'
        entity_level_total_recall(candidates, gold_file, 'stg_temp_min')
    """
    docs = [(doc.name).upper() for doc in corpus.documents] if corpus else None
    gold_set = get_gold_dict(gold_file, docs=docs, doc_on=True, part_on=True, val_on=relation, attrib=attribute, integerize=integerize)
    if len(gold_set) == 0:
        print "Gold set is empty."
        return
    # Turn CandidateSet into set of tuples
    print "Preparing candidates..."
    pb = ProgressBar(len(candidates))
    entity_level_candidates = set()
    for i, c in enumerate(candidates):
        pb.bar(i)
        part = c.get_arguments()[0].get_span()
        doc = c.get_arguments()[0].parent.document.name.upper()
        if relation:
            val = c.get_arguments()[1].get_span()
            # if integerize:
            #   val = int(float(c.get_arguments()[1].get_span().replace(' ', '')))
        for p in get_implied_parts(part, doc, parts_by_doc):
            if relation:
                entity_level_candidates.add((doc, part, val))
            else:
                entity_level_candidates.add((doc, part))
    pb.close()

    (TP_set, FP_set, FN_set) = entity_confusion_matrix(entity_level_candidates, gold_set)
    TP = len(TP_set)
    FP = len(FP_set)
    FN = len(FN_set)

    prec = TP / float(TP + FP) if TP + FP > 0 else float('nan')
    rec  = TP / float(TP + FN) if TP + FN > 0 else float('nan')
    f1   = 2 * (prec * rec) / (prec + rec) if prec + rec > 0 else float('nan')
    print "========================================"
    print "Scoring on Entity-Level Gold Data"
    print "========================================"
    print "Corpus Precision {:.3}".format(prec)
    print "Corpus Recall    {:.3}".format(rec)
    print "Corpus F1        {:.3}".format(f1)
    print "----------------------------------------"
    print "TP: {} | FP: {} | FN: {}".format(TP, FP, FN)
    print "========================================\n"
    return map(lambda x: sorted(list(x)), [TP_set, FP_set, FN_set])


def entity_level_f1(tp, fp, tn, fn, gold_file, corpus, attrib):
    docs = [(doc.name).upper() for doc in corpus.documents] if corpus else None
    gold_dict = get_gold_dict(gold_file, docs=docs, doc_on=True, part_on=(attrib is not None), val_on=True, attrib=attrib)

    TP = FP = TN = FN = 0
    pos = set([((c[0].parent.document.name).upper(),
                (c[0].get_span()).upper(),
                (''.join(c[1].get_span().split())).upper()) for c in tp.union(fp)])
    TP_set = pos.intersection(gold_dict)
    TP = len(TP_set)
    FP_set = pos.difference(gold_dict)
    FP = len(FP_set)
    FN_set = gold_dict.difference(pos)
    FN = len(FN_set)

    prec = TP / float(TP + FP) if TP + FP > 0 else float('nan')
    rec  = TP / float(TP + FN) if TP + FN > 0 else float('nan')
    f1   = 2 * (prec * rec) / (prec + rec) if prec + rec > 0 else float('nan')
    print "========================================"
    print "Scoring on Entity-Level Gold Data"
    print "========================================"
    print "Corpus Precision {:.3}".format(prec)
    print "Corpus Recall    {:.3}".format(rec)
    print "Corpus F1        {:.3}".format(f1)
    print "----------------------------------------"
    print "TP: {} | FP: {} | FN: {}".format(TP, FP, FN)
    print "========================================\n"
    return map(lambda x: sorted(list(x)), [TP_set, FP_set, FN_set])

def get_implied_parts(part, doc, parts_by_doc):
    yield part
    if parts_by_doc:
        for p in parts_by_doc[doc]:
            if p.startswith(part) and len(part) >= 4:
                yield p

def parts_f1(candidates, gold_parts, parts_by_doc=None):
    parts = set()
    for c in candidates:
        doc = c.part.parent.document.name.upper()
        part = c.part.get_span()
        for p in get_implied_parts(part, doc, parts_by_doc):
            parts.add((doc, p))
    # parts = set([(c.part.parent.document.name.upper(), c.part.get_span()) for c in candidates])
    # import pdb; pdb.set_trace()
    TP_set = parts.intersection(gold_parts)
    TP = len(TP_set)
    FP_set = parts.difference(gold_parts)
    FP = len(FP_set)
    FN_set = gold_parts.difference(parts)
    FN = len(FN_set)
    prec = TP / float(TP + FP) if TP + FP > 0 else float('nan')
    rec  = TP / float(TP + FN) if TP + FN > 0 else float('nan')
    f1   = 2 * (prec * rec) / (prec + rec) if prec + rec > 0 else float('nan')
    print "========================================"
    print "Scoring on Entity-Level Gold Data"
    print "========================================"
    print "Corpus Precision {:.3}".format(prec)
    print "Corpus Recall    {:.3}".format(rec)
    print "Corpus F1        {:.3}".format(f1)
    print "----------------------------------------"
    print "TP: {} | FP: {} | FN: {}".format(TP, FP, FN)
    print "========================================\n"
    return map(lambda x: sorted(list(x)), [TP_set, FP_set, FN_set])

def expand_part_range(text, DEBUG=False):
    """
    Given a string, generates strings that are potentially implied by
    the original text. Two main operations are performed:
        1. Expanding ranges (X to Y; X ~ Y; X -- Y)
        2. Expanding suffixes (123X/Y/Z; 123X, Y, Z)
    Also yields the original input string.
    To get the correct output from complex strings, this function should be fed
    many Ngrams from a particular phrase.
    """
    ### Regex Patterns compile only once per function call.
    # This range pattern will find text that "looks like" a range.
    range_pattern = re.compile(ur'^(?P<start>[\w\/]+)(?:\s*(\.{3,}|\~|\-+|to|thru|through|\u2011+|\u2012+|\u2013+|\u2014+|\u2012+|\u2212+)\s*)(?P<end>[\w\/]+)$', re.IGNORECASE | re.UNICODE)
    suffix_pattern = re.compile(ur'(?P<spacer>(?:,|\/)\s*)(?P<suffix>[\w\-]+)')
    base_pattern = re.compile(ur'(?P<base>[\w\-]+)(?P<spacer>(?:,|\/)\s*)(?P<suffix>[\w\-]+)?')

    if DEBUG: print "\n[debug] Text: " + text
    expanded_parts = set()
    final_set = set()

    ### Step 1: Search and expand ranges
    m = re.search(range_pattern, text)
    if m:
        start = m.group("start")
        end = m.group("end")
        start_diff = ""
        end_diff = ""
        if DEBUG: print "[debug]   Start: %s \t End: %s" % (start, end)

        # Use difflib to find difference. We are interested in 'replace' only
        seqm = SequenceMatcher(None, start, end).get_opcodes();
        for opcode, a0, a1, b0, b1 in seqm:
            if opcode == 'equal':
                continue
            elif opcode == 'insert':
                break
            elif opcode == 'delete':
                break
            elif opcode == 'replace':
                # NOTE: Potential bug if there is more than 1 replace
                start_diff = start[a0:a1]
                end_diff = end[b0:b1]
            else:
                raise RuntimeError, "[ERROR] unexpected opcode"

        if DEBUG: print "[debug]   start_diff: %s \t end_diff: %s" % (start_diff, end_diff)

        # First, check for number range
        if atoi(start_diff) and atoi(end_diff):
            if DEBUG: print "[debug]   Enumerate %d to %d" % (atoi(start_diff), atoi(end_diff))
            # generate a list of the numbers plugged in
            for number in xrange(atoi(start_diff), atoi(end_diff) + 1):
                new_part = start.replace(start_diff,str(number))
                # Produce the strings with the enumerated ranges
                expanded_parts.add(new_part)

        # Second, check for single-letter enumeration
        if len(start_diff) == 1 and len(end_diff) == 1:
            if start_diff.isalpha() and end_diff.isalpha():
                if DEBUG: print "[debug]   Enumerate %s to %s" % (start_diff, end_diff)
                letter_range = char_range(start_diff, end_diff)
                for letter in letter_range:
                    new_part = start.replace(start_diff,letter)
                    # Produce the strings with the enumerated ranges
                    expanded_parts.add(new_part)

        # If we cannot identify a clear number or letter range, or if there are
        # multiple ranges being expressed, just ignore it.
        if len(expanded_parts) == 0:
            expanded_parts.add(text)
    else:
        expanded_parts.add(text)
        # Special case is when there is a single slack (e.g. BC337-16/BC338-16)
        # and we want to output both halves of the slash, assuming that both
        # halves are the same length
        if text.count('/') == 1:
            split = text.split('/')
            if len(split[0]) == len(split[1]):
                expanded_parts.add(split[0])
                expanded_parts.add(split[1])


    if DEBUG: print "[debug]   Inferred Text: \n  " + str(sorted(expanded_parts))

    ### Step 2: Expand suffixes for each of the inferred phrases
    # NOTE: this only does the simple case of replacing same-length suffixes.
    # we do not handle cases like "BC546A/B/XYZ/QR"
    for part in expanded_parts:
        first_match = re.search(base_pattern, part)
        if first_match:
            base = re.search(base_pattern, part).group("base");
            final_set.add(base) # add the base (multiple times, but set handles that)
            if (first_match.group("suffix")):
                all_suffix_lengths = set()
                # This is a bit inefficient but this first pass just is here
                # to make sure that the suffixes are the same length
                # first_suffix = first_match.group("suffix")
                # if part.startswith('BC547'):
                #     import pdb; pdb.set_trace()
                for m in re.finditer(suffix_pattern, part):
                    suffix = m.group("suffix")
                    suffix_len = len(suffix)
                    all_suffix_lengths.add(suffix_len)
                if len(all_suffix_lengths) == 1:
                    for m in re.finditer(suffix_pattern, part):
                        spacer = m.group("spacer")
                        suffix = m.group("suffix")
                        suffix_len = len(suffix)
                        old_suffix = base[-suffix_len:]
                        if ((suffix.isalpha() and old_suffix.isalpha()) or 
                            (suffix.isnumeric() and old_suffix.isnumeric())):
                            trimmed = base[:-suffix_len]
                            final_set.add(trimmed+suffix)
        else:
            if part and (not part.isspace()):
                final_set.add(part) # no base was found with suffixes to expand
    if DEBUG: print "[debug]   Final Set: " + str(sorted(final_set))

    # Also return the original input string
    final_set.add(text)

    for part in final_set:
        yield part

    # Add common part suffixes on each discovered part number
    # part_suffixes = ['-16','-25','-40','A','B','C']
    # for part in final_set:
    #     base = part
    #     for suffix in part_suffixes:
    #         if part.endswith(suffix):
    #             base = part[:-len(suffix)].replace(' ', '') # e.g., for parts in SIEMS01215-1
    #             break
    #     if base:
    #         yield base
    #         for suffix in part_suffixes:
    #             yield base + suffix
    #     else:
    #         yield part

    # NOTE: We make a few assumptions (e.g. suffixes must be same length), but
    # one important unstated assumption is that if there is a single suffix,
    # (e.g. BC546A/B), the single suffix will be swapped in no matter what.
    # In this example, it works. But if we had "ABCD/EFG" we would get "ABCD,AEFG"
    # Check out UtilsTests.py to see more of our assumptions capture as test
    # cases.


def atoi(num_str):
    '''
    Helper function which converts a string to an integer, or returns None.
    '''
    try:
        return int(num_str)
    except:
        pass
    return None


def char_range(a, b):
    '''
    Generates the characters from a to b inclusive.
    '''
    for c in xrange(ord(a), ord(b)+1):
        yield chr(c)


def candidate_to_entity(candidate):
    part = candidate.get_arguments()[0]
    attr = candidate.get_arguments()[1]
    doc  = part.parent.document.name
    return (doc.upper(), part.get_span().upper(), attr.get_span().upper())


def candidates_to_entities(candidates):
    entities = set()
    pb = ProgressBar(len(candidates))
    for i, c in enumerate(candidates):
        pb.bar(i)
        entities.add(candidate_to_entity(c))
    pb.close()
    return entities


def entity_to_candidates(entity, candidate_subset):
    matches = []
    for c in candidate_subset:
        c_entity = tuple([c[0].parent.document.name.upper()] + [c[i].get_span().upper() for i in range(len(c))])
        if c_entity == entity:
        # (part, attr) = c.get_arguments()
        # if (c[0].parent.document.name.upper(), part.get_span().upper(), attr.get_span().upper()) == entity:
            matches.append(c)
    return matches


def count_labels(entities, gold):
    T = 0
    F = 0
    for e in entities:
        if e in gold:
            T += 1
        else:
            F += 1
    return (T, F)


def part_error_analysis(c):
    print "Doc: %s" % c.part.parent.document
    print "------------"
    part = c.get_arguments()[0]
    print "Part:"
    print part
    print part.parent
    print "------------"
    attr = c.get_arguments()[1]
    print "Attr:"
    print attr
    print attr.parent
    print "------------"


def print_table_info(span):
    print "------------"
    if span.parent.table:
        print "Table: %s" % span.parent.table
    if span.parent.cell:
        print "Row: %s" % span.parent.row_start
        print "Col: %s" % span.parent.col_start
    print "Phrase: %s" % span.parent


def get_gold_parts_by_doc():
    gold_file = os.environ['SNORKELHOME'] + '/tutorials/tables/data/hardware/dev/hardware_dev_gold.csv'
    gold_parts = get_gold_dict(gold_file, doc_on=True, part_on=True, val_on=False)
    parts_by_doc = defaultdict(set)
    for part in gold_parts:
        parts_by_doc[part[0]].add(part[1])
    return parts_by_doc

def get_manual_parts_by_doc(documents):
    eeca_suffix = '^(A|B|C|R|O|Y|-?16|-?25|-?40)$'
    suffix_matcher = RegexMatchSpan(rgx=eeca_suffix, ignore_case=False)
    suffix_ngrams = OmniNgrams(n_max=1)
    part_ngrams = OmniNgramsPart(n_max=5)
    return generate_parts_by_doc(documents, 
                                 part_matcher=get_part_matcher(), 
                                 part_ngrams=part_ngrams, 
                                 suffix_matcher=suffix_matcher, 
                                 suffix_ngrams=suffix_ngrams)      


def merge_two_dicts(x, y):
    '''Given two dicts, merge them into a new dict as a shallow copy.'''
    # Code from http://stackoverflow.com/questions/38987/how-to-merge-two-python-dictionaries-in-a-single-expression
    # Note that the entries of Y will replace X's values if there is overlap.
    z = x.copy()
    z.update(y)
    return z

def generate_parts_by_doc(contexts, part_matcher, part_ngrams, suffix_matcher, suffix_ngrams):
    """
    Seeks to replace get_gold_dict by going through a first pass of the document
    and pull out valid part numbers.

    Note that some throttling is done here, but may be moved to either a Throttler
    class, or learned through using LFs. Throttling here just seeks to reduce
    the number of candidates produced.

    Note that part_ngrams should be at least 5-grams or else not all parts will
    be found.
    """
    suffixes_by_doc = defaultdict(set)
    parts_by_doc = defaultdict(set)

    print "Finding part numbers..."
    pb = ProgressBar(len(contexts))
    for i, context in enumerate(contexts):
        pb.bar(i)
        # extract parts
        for ts in part_ngrams.apply(context):
            # identify parts
            for pts in part_matcher.apply([ts]):
                parts_by_doc[pts.parent.document.name.upper()].add(pts.get_span())

            # identify suffixes
            for sts in suffix_matcher.apply([ts]):
                row_ngrams = set(get_row_ngrams(ts, infer=True))
                if ('classification' in row_ngrams or
                    'group' in row_ngrams or
                    'rank' in row_ngrams or
                    'grp.' in row_ngrams):
                    suffixes_by_doc[sts.parent.document.name.upper()].add(sts.get_span())
    pb.close()

    print suffixes_by_doc

    # Process suffixes and parts
    print "Appending suffixes..."
    final_dict = defaultdict(set)
    pb = ProgressBar(len(parts_by_doc))
    for doc in parts_by_doc.keys():
        pb.bar(i)
        for part in parts_by_doc[doc]:
            final_dict[doc].add(part)
            # TODO: This portion is really specific to our suffixes. Ideally
            # this kind of logic can be pased on the suffix_matcher that is
            # pass in. Or something like that...
            # The goal of this code is just to append suffixes to part numbers
            # that don't already have suffixes in a reasonable way.
            suffixes = suffixes_by_doc[doc]
            if not any(s in part[4:] for s in suffixes):
                for s in suffixes:
                    if s.isdigit(): s = '-' + s
                    final_dict[doc].add(part + s)
            # for suffix in suffixes:
            #     """
            #     if the part has no suffix, add it
            #     """
            #     if 
            #     if (suffix == "A" or suffix == "B" or suffix == "C"): # suffix.isalpha()?
            #         if not any(x in part[2:] for x in ['A', 'B', 'C']):
            #             final_dict[doc].add(part + suffix)
            #     else: # if it's 16/25/40
            #         if not suffix.startswith('-') and not any(x in part for x in ['16', '25', '40']):
            #             final_dict[doc].add(part + '-' + suffix)
            #         elif suffix.startswith('-') and not any(x in part for x in ['16', '25', '40']):
            #             final_dict[doc].add(part + suffix)
    pb.close()
    return final_dict

# HOLD ON TO THESE FOR REFERENCE UNTIL HARDWARE NOTEBOOKS ARE UPDATED
# class PartThrottler(object):
#     """
#     Removes candidates unless the part is not in a table, or the part aligned
#     temperature are not aligned.
#     """
#     def apply(self, part_span, attr_span):
#         """
#         Returns True is the tuple passes, False if it should be throttled
#         """
#         return part_span.parent.table is None or self.aligned(part_span, attr_span)

#     def aligned(self, span1, span2):
#         return (span1.parent.table == span2.parent.table and
#             (span1.parent.row_num == span2.parent.row_num or
#              span1.parent.col_num == span2.parent.col_num))

# class GainThrottler(PartThrottler):
#     def apply(self, part_span, attr_span):
#         """
#         Returns True is the tuple passes, False if it should be throttled
#         """
#         return (PartThrottler.apply(self, part_span, attr_span) and
#             overlap(['dc', 'gain', 'hfe', 'fe'], list(get_row_ngrams(attr_span, infer=True))))

# class PartCurrentThrottler(object):
#     """
#     Removes candidates unless the part is not in a table, or the part aligned
#     temperature are not aligned.
#     """
#     def apply(self, part_span, current_span):
#         """
#         Returns True is the tuple passes, False if it should be throttled
#         """
#         # if both are in the same table
#         if (part_span.parent.table is not None and current_span.parent.table is not None):
#             if (part_span.parent.table == current_span.parent.table):
#                 return True

#         # if part is in header, current is in table
#         if (part_span.parent.table is None and current_span.parent.table is not None):
#             ngrams = set(get_row_ngrams(current_span))
#             # if True:
#             if ('collector' in ngrams and 'current' in ngrams):
#                 return True

#         # if neither part or current is in table
#         if (part_span.parent.table is None and current_span.parent.table is None):
#             ngrams = set(get_phrase_ngrams(current_span))
#             num_numbers = list(get_phrase_ngrams(current_span, attrib="ner_tags")).count('number')
#             if ('collector' in ngrams and 'current' in ngrams and num_numbers <= 3):
#                 return True

#         return False

#     def aligned(self, span1, span2):
#         ngrams = set(get_row_ngrams(span2))
#         return  (span1.parent.table == span2.parent.table and
#             (span1.parent.row_num == span2.parent.row_num or span1.parent.col_num == span2.parent.col_num))
