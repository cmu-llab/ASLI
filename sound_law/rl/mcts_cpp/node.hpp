#pragma once

#include "common.hpp"
#include "word.hpp"

namespace node
{
    constexpr int END_DEPTH = -1;
}

// This enum class documents which phase a node is in, in terms of finishing sampling an action.
enum class ActionPhase : int
{
    BEFORE,
    AFTER,
    PRE,
    D_PRE,
    POST,
    SPECIAL_TYPE,
};

using Affected = vec<pair<int, size_t>>;
using ChosenChar = pair<int, abc_t>;

class ActionSpace;
class Env;

/* ------------------------- Base Node ------------------------ */

class BaseNode
{
    friend class ActionSpace;
    friend class Env;
    friend class EdgeBuilder;
    friend class Traverser;

    // void connect(BaseNode *const, const ChosenChar &);
    vec<BaseNode *> parents;
    vec<size_t> parent_indices;

    size_t in_degree = 0;
    bool visited = false;

    // Disconnect the node from all of its parents.
    void disconnect_from_parents();
    // Disconnect the node from all of its children.
    void disconnect_from_children();

protected:
    std::mutex mtx;

    BaseNode(BaseNode *const, const ChosenChar &, bool, bool);

    bool played = false;

    // Given the current action phase, get the best next mini node.
    ChosenChar get_best_subaction(float, int, float, float);

public:
    virtual ~BaseNode() = default;

    const bool stopped;
    const bool persistent;

    vec<abc_t> permissible_chars; // What characters are permissible to act upon?
    vec<Affected> affected;       // What positions are affected by each permissible character?
    vec<BaseNode *> children;

    vec<float> priors;
    vec<bool> pruned;
    vec<visit_t> action_counts;
    vec<float> total_values;
    vec<float> max_values;
    visit_t visit_count = 0;
    int max_index = -1;
    float max_value = -9999.9;
    int num_unpruned_actions = -1;

    bool is_expanded();
    bool is_evaluated();
    pair<BaseNode *, ChosenChar> play_mini();
    vec<float> get_scores(float, float);
    size_t get_num_actions();
    void prune(int);
    void prune();
    bool is_pruned();
    // size_t get_num_descendants();

    bool has_child(size_t) const;
    BaseNode *get_child(size_t) const;
    int get_in_degree() const;

    virtual bool is_transitional() = 0;
    virtual bool is_tree_node() = 0;
};

/* ------------------------- Mini Node ------------------------ */

class TreeNode;

class MiniNode : public BaseNode
{
    friend class ActionSpace;
    friend class TreeNode;
    friend class TransitionNode;

    MiniNode(TreeNode *, BaseNode *const, const ChosenChar &, ActionPhase, bool);

public:
    virtual ~MiniNode() override = default;

    TreeNode *const base;
    const ActionPhase ap;

    bool is_tree_node() override;

    virtual bool is_transitional();
};

/* ---------------------- Transition Node --------------------- */
// This is the last mini node that leads to a normal tree node. Only this node has rewards.

class TransitionNode : public MiniNode
{
    friend class ActionSpace;
    friend class Env;

    TransitionNode(TreeNode *, MiniNode *, const ChosenChar &, bool);

public:
    ~TransitionNode() override = default;

    vec<float> rewards;

    bool is_transitional() override;
};

/* ------------------------- Tree Node ------------------------ */

struct Subpath
{
    array<ChosenChar, 7> chosen_seq;
    array<MiniNode *, 6> mini_node_seq;
    bool stopped;

    void connect(TreeNode *) const;
};

class Mcts;
class LruCache;

class TreeNode : public BaseNode
{
    friend class Mcts;
    friend class Env;
    friend class ActionSpace;
    friend class LruCache;

    static Trie<Word *, TreeNode *> t_table;
    static TreeNode *get_tree_node(const vec<Word *> &, int);
    static TreeNode *get_tree_node(const vec<Word *> &, int, BaseNode *const, const ChosenChar &, bool);

    vec<vec<float>> meta_priors;
    vec<float> special_priors;

    void common_init(const vec<Word *> &);
    // This is used for persistent nodes (e.g., start and end nodes).
    TreeNode(const vec<Word *> &, int);
    // This is used for everything else.
    TreeNode(const vec<Word *> &, int, BaseNode *const, const ChosenChar &, bool);

    pair<TreeNode *, Subpath> play();

public:
    ~TreeNode() override = default;

    const vec<Word *> words;
    const int depth;

    float dist = 0.0;
    bool done = false;

    // Return a vector of `MiniNode *` as the subactions.
    bool is_leaf();
    IdSeq get_id_seq(int);
    size_t size();
    bool is_transitional() override;
    bool is_tree_node() override;
};

namespace str
{
    inline string from(ActionPhase ap)
    {
        switch (ap)
        {
        case ActionPhase::BEFORE:
            return "BEFORE";
        case ActionPhase::AFTER:
            return "AFTER";
        case ActionPhase::PRE:
            return "PRE";
        case ActionPhase::D_PRE:
            return "D_PRE";
        case ActionPhase::POST:
            return "D_POST";
        case ActionPhase::SPECIAL_TYPE:
            return "SPECIAL_TYPE";
        }
    }

    inline string from(BaseNode *node) { return "stopped: " + from(node->stopped); };

    inline string from(TreeNode *node)
    {
        string out = from(static_cast<BaseNode *>(node)) + "\n";
        for (const auto word : node->words)
        {
            for (const auto unit : word->id_seq)
                out += std::to_string(unit) + " ";
            out += "\n";
        }
        return out;
    }

    inline string from(MiniNode *node) { return from(static_cast<BaseNode *>(node)) + " phase: " + from(node->ap) + " base: " + from(node->base); }

} // namespace str

class EdgeBuilder
{
    friend class LruCache;
    friend class ActionSpace;

private:
    // Connect `parent` and `child` through `parent`'s action at `index`. Assume they are not connected before.
    static void connect(BaseNode *parent, size_t index, BaseNode *child)
    {
        assert(parent->children[index] == nullptr);
        parent->children[index] = child;
        child->parents.push_back(parent);
        child->parent_indices.push_back(index);
        ++child->in_degree;
    }

    // Disconnet `node` from all of its children and parents.
    static void disconnect(BaseNode *node)
    {
        node->disconnect_from_parents();
        node->disconnect_from_children();
    }
};

class Traverser
{
    friend class ActionSpace;

private:
    // Visit one node and append it to the queue if it hasn't been visited.
    static void visit(BaseNode *node, vec<BaseNode *> &queue)
    {
        if (!node->visited)
        {
            node->visited = true;
            queue.push_back(node);
        }
    };

    // Traverse from `start` using bfs.
    static vec<BaseNode *> bfs(BaseNode *start)
    {
        auto queue = vec<BaseNode *>();
        visit(start, queue);
        size_t i = 0;
        while (i < queue.size())
        {
            auto selected = queue[i];
            for (const auto child : selected->children)
                if (child != nullptr)
                    visit(child, queue);
            ++i;
        }

        for (const auto node : queue)
            node->visited = false;
        return queue;
    }
};