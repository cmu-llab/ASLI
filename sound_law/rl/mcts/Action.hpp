#pragma once

#include "common.hpp"
#include "SiteGraph.hpp"
#include "Word.hpp"
#include "ctpl.h"

class TreeNode;

class ActionSpace
{
public:
    ActionSpace(SiteSpace *, WordSpace *, float, int);

    SiteSpace *site_space;
    WordSpace *word_space;
    const float prune_threshold;

    void register_edge(abc_t, abc_t);
    void set_action_allowed(TreeNode *);
    void find_potential_actions(TreeNode *,
                                std::vector<uai_t> &,
                                std::vector<std::vector<int>> &,
                                std::vector<uai_t> &,
                                std::vector<std::vector<int>> &);

private:
    void set_action_allowed_no_lock(TreeNode *);

    ctpl::thread_pool tp;
    UMap<abc_t, std::vector<abc_t>> edges;
};